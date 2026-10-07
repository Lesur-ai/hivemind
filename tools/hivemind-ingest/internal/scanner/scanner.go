package scanner

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

// FileItem represents a discovered file with its metadata
type FileItem struct {
	Path        string `json:"path"`
	RelPath     string `json:"rel_path"`
	Filename    string `json:"filename"`
	Size        int64  `json:"size"`
	SHA256      string `json:"sha256"`
	ContentType string `json:"content_type"`
	IsText      bool   `json:"is_text"`
}

// Batch represents a chunk of files grouped by size limit
type Batch struct {
	Index     int        `json:"index"`
	Files     []FileItem `json:"files"`
	TotalSize int64      `json:"total_size"`
}

// ScanOptions configures the directory traversal and file selection
type ScanOptions struct {
	RootPath          string
	AllowedExtensions []string
	BatchSizeMB       int
	MaxFileBytes      int64
	KnownDocuments    map[string]string // map of source_path -> sha256
	KnownHashes       map[string]bool   // deprecated hash-only fallback
	ForceReplace      bool
}

// ScanResult contains discovered items and batches
type ScanResult struct {
	RootPath     string   `json:"root_path"`
	TotalFiles   int      `json:"total_files"`
	TotalBytes   int64    `json:"total_bytes"`
	Batches      []Batch  `json:"batches"`
	SkippedCount int      `json:"skipped_count"`
	SkippedFiles []string `json:"skipped_files,omitempty"`
}

// FileContent holds loaded data for a single file on-demand with strictly bound SHA-256
type FileContent struct {
	TextContent string
	Base64Data  string
	IsText      bool
	SHA256      string
}

// LoadFileContent reads and encodes file content on-demand, refusing symlinks and computing SHA-256 directly on the opened descriptor.
func LoadFileContent(path string, maxBytes int64) (*FileContent, error) {
	if maxBytes <= 0 {
		maxBytes = 50 * 1024 * 1024
	}

	lstat, err := os.Lstat(path)
	if err != nil {
		return nil, fmt.Errorf("failed to stat file %s: %w", path, err)
	}
	if lstat.Mode()&os.ModeSymlink != 0 || !lstat.Mode().IsRegular() {
		return nil, fmt.Errorf("file %s is not a regular file (symlinks are forbidden)", path)
	}
	if lstat.Size() > maxBytes {
		return nil, fmt.Errorf("file %s size (%d bytes) exceeds maximum allowed limit (%d bytes)", path, lstat.Size(), maxBytes)
	}

	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("failed to open file %s: %w", path, err)
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("failed to stat descriptor %s: %w", path, err)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("file %s descriptor is not a regular file", path)
	}
	if !os.SameFile(lstat, info) {
		return nil, fmt.Errorf("file %s was replaced during open (TOCTOU detected)", path)
	}

	limitR := io.LimitReader(f, maxBytes+1)
	data, err := io.ReadAll(limitR)
	if err != nil {
		return nil, fmt.Errorf("failed to read file %s: %w", path, err)
	}
	if int64(len(data)) > maxBytes {
		return nil, fmt.Errorf("file %s size exceeded maximum allowed limit (%d bytes) during read", path, maxBytes)
	}

	sha := sha256.Sum256(data)
	hashHex := hex.EncodeToString(sha[:])
	b64 := base64.StdEncoding.EncodeToString(data)

	// Empty file is treated as empty text
	if len(data) == 0 {
		return &FileContent{
			TextContent: "",
			Base64Data:  "",
			IsText:      true,
			SHA256:      hashHex,
		}, nil
	}

	// Check for binary NUL bytes or invalid UTF-8
	isBinary := bytes.IndexByte(data, 0) != -1 || !utf8.Valid(data)
	if isBinary {
		return &FileContent{
			TextContent: "",
			Base64Data:  b64,
			IsText:      false,
			SHA256:      hashHex,
		}, nil
	}

	return &FileContent{
		TextContent: string(data),
		Base64Data:  b64,
		IsText:      true,
		SHA256:      hashHex,
	}, nil
}

// Scan traverses the target path and prepares file batches (metadata only, no persistent buffer OOM)
func Scan(opts ScanOptions) (*ScanResult, error) {
	if opts.RootPath == "" {
		return nil, fmt.Errorf("root path is required")
	}

	info, err := os.Lstat(opts.RootPath)
	if err != nil {
		return nil, fmt.Errorf("failed to access %s: %w", opts.RootPath, err)
	}

	extMap := make(map[string]bool)
	for _, ext := range opts.AllowedExtensions {
		e := strings.ToLower(strings.TrimSpace(ext))
		if e != "" {
			if !strings.HasPrefix(e, ".") {
				e = "." + e
			}
			extMap[e] = true
		}
	}

	var discovered []FileItem
	var skipped []string

	maxBatchBytes := int64(opts.BatchSizeMB) * 1024 * 1024
	if maxBatchBytes <= 0 {
		maxBatchBytes = 50 * 1024 * 1024
	}
	maxFileBytes := opts.MaxFileBytes
	if maxFileBytes <= 0 {
		maxFileBytes = maxBatchBytes
	}

	processFile := func(fullPath string, fInfo os.FileInfo, relPath string) error {
		// Strict regular file check: reject symlinks, FIFOs, devices, sockets
		if !fInfo.Mode().IsRegular() {
			return nil
		}

		if fInfo.Size() > maxFileBytes {
			return fmt.Errorf("file %s size (%d bytes) exceeds maximum allowed limit (%d bytes)", fullPath, fInfo.Size(), maxFileBytes)
		}

		base := fInfo.Name()
		if strings.HasPrefix(base, ".") {
			return nil
		}

		ext := strings.ToLower(filepath.Ext(base))
		if len(extMap) > 0 && !extMap[ext] {
			return nil
		}

		f, err := os.Open(fullPath)
		if err != nil {
			return fmt.Errorf("failed to open file %s: %w", fullPath, err)
		}
		defer f.Close()

		st, err := f.Stat()
		if err != nil || !st.Mode().IsRegular() {
			return nil
		}
		if !os.SameFile(fInfo, st) {
			return fmt.Errorf("file %s was replaced during open (TOCTOU detected)", fullPath)
		}

		// Stream SHA-256 computation and header sniffing with bounded reader
		hasher := sha256.New()
		headerBuf := make([]byte, 512)
		nHeader, _ := io.ReadFull(f, headerBuf)
		if nHeader > 0 {
			hasher.Write(headerBuf[:nHeader])
		}
		limitR := io.LimitReader(f, maxFileBytes+1-int64(nHeader))
		nCopied, err := io.Copy(hasher, limitR)
		if err != nil {
			return fmt.Errorf("failed to compute hash for %s: %w", fullPath, err)
		}
		if int64(nHeader)+nCopied > maxFileBytes {
			return fmt.Errorf("file %s size exceeded maximum allowed limit (%d bytes) during read", fullPath, maxFileBytes)
		}
		hashHex := hex.EncodeToString(hasher.Sum(nil))

		// Check if known and not forcing replace
		if !opts.ForceReplace {
			if opts.KnownDocuments != nil {
				if existingHash, ok := opts.KnownDocuments[relPath]; ok && strings.EqualFold(existingHash, hashHex) {
					skipped = append(skipped, relPath)
					return nil
				}
			} else if opts.KnownHashes != nil && opts.KnownHashes[hashHex] {
				skipped = append(skipped, relPath)
				return nil
			}
		}

		cType := mime.TypeByExtension(ext)
		if cType == "" && nHeader > 0 {
			cType = http.DetectContentType(headerBuf[:nHeader])
		}
		if cType == "" {
			cType = "application/octet-stream"
		}

		isText := true
		if nHeader > 0 {
			if bytes.IndexByte(headerBuf[:nHeader], 0) != -1 || !utf8.Valid(headerBuf[:nHeader]) {
				isText = false
			}
		}

		item := FileItem{
			Path:        fullPath,
			RelPath:     relPath,
			Filename:    base,
			Size:        fInfo.Size(),
			SHA256:      hashHex,
			ContentType: cType,
			IsText:      isText,
		}

		discovered = append(discovered, item)
		return nil
	}

	if !info.IsDir() {
		if !info.Mode().IsRegular() {
			return nil, fmt.Errorf("target path %s is not a regular file", opts.RootPath)
		}
		relPath := filepath.Base(opts.RootPath)
		if err := processFile(opts.RootPath, info, relPath); err != nil {
			return nil, err
		}
	} else {
		err := filepath.Walk(opts.RootPath, func(path string, fInfo os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			// Skip hidden directories explicitly with SkipDir
			if fInfo.IsDir() {
				base := fInfo.Name()
				if strings.HasPrefix(base, ".") && path != opts.RootPath {
					return filepath.SkipDir
				}
				return nil
			}

			relPath, errRel := filepath.Rel(opts.RootPath, path)
			if errRel != nil {
				relPath = path
			}
			return processFile(path, fInfo, relPath)
		})
		if err != nil {
			return nil, fmt.Errorf("error walking path %s: %w", opts.RootPath, err)
		}
	}

	// Partition into batches
	var batches []Batch
	var currentBatch []FileItem
	var currentSize int64
	batchIndex := 0

	var totalBytes int64
	for _, item := range discovered {
		totalBytes += item.Size
		if len(currentBatch) > 0 && ((currentSize+item.Size) > maxBatchBytes || len(currentBatch) >= 200) {
			batches = append(batches, Batch{
				Index:     batchIndex,
				Files:     currentBatch,
				TotalSize: currentSize,
			})
			batchIndex++
			currentBatch = nil
			currentSize = 0
		}
		currentBatch = append(currentBatch, item)
		currentSize += item.Size
	}

	if len(currentBatch) > 0 {
		batches = append(batches, Batch{
			Index:     batchIndex,
			Files:     currentBatch,
			TotalSize: currentSize,
		})
	}

	return &ScanResult{
		RootPath:     opts.RootPath,
		TotalFiles:   len(discovered),
		TotalBytes:   totalBytes,
		Batches:      batches,
		SkippedCount: len(skipped),
		SkippedFiles: skipped,
	}, nil
}
