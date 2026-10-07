package scanner

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func TestScanner(t *testing.T) {
	tempDir := t.TempDir()

	f1 := filepath.Join(tempDir, "doc1.md")
	f2 := filepath.Join(tempDir, "doc2.txt")
	f3 := filepath.Join(tempDir, "ignored.bin")
	subDir := filepath.Join(tempDir, "nested")
	_ = os.MkdirAll(subDir, 0755)
	f4 := filepath.Join(subDir, "doc3.md")

	_ = os.WriteFile(f1, []byte("# Title 1\nHello world"), 0644)
	_ = os.WriteFile(f2, []byte("Simple plain text"), 0644)
	_ = os.WriteFile(f3, []byte("binary data"), 0644)
	_ = os.WriteFile(f4, []byte("# Nested doc"), 0644)

	opts := ScanOptions{
		RootPath:          tempDir,
		AllowedExtensions: []string{".md", ".txt"},
		BatchSizeMB:       1,
	}

	res, err := Scan(opts)
	if err != nil {
		t.Fatalf("Scan failed: %v", err)
	}

	if res.TotalFiles != 3 {
		t.Errorf("expected 3 files, got %d", res.TotalFiles)
	}
	if len(res.Batches) == 0 {
		t.Fatalf("expected at least 1 batch")
	}

	// Test skip unchanged hashes
	known := make(map[string]bool)
	for _, batch := range res.Batches {
		for _, file := range batch.Files {
			if file.Filename == "doc1.md" {
				known[file.SHA256] = true
			}
		}
	}

	opts.KnownHashes = known
	res2, err := Scan(opts)
	if err != nil {
		t.Fatalf("Scan2 failed: %v", err)
	}
	if res2.TotalFiles != 2 {
		t.Errorf("expected 2 files after skipping doc1.md, got %d", res2.TotalFiles)
	}
	if res2.SkippedCount != 1 {
		t.Errorf("expected 1 skipped file, got %d", res2.SkippedCount)
	}
}

func TestScannerRejectsSymlinksAndHiddenDirs(t *testing.T) {
	tempDir := t.TempDir()

	// 1. Regular valid file
	validFile := filepath.Join(tempDir, "valid.md")
	_ = os.WriteFile(validFile, []byte("# Valid doc"), 0644)

	// 2. Hidden dir with file inside
	hiddenDir := filepath.Join(tempDir, ".hidden")
	_ = os.MkdirAll(hiddenDir, 0755)
	hiddenFile := filepath.Join(hiddenDir, "secret.md")
	_ = os.WriteFile(hiddenFile, []byte("# Secret in hidden dir"), 0644)

	// 3. Target outside dir and symlink to it
	outsideDir := t.TempDir()
	outsideFile := filepath.Join(outsideDir, "outside.md")
	_ = os.WriteFile(outsideFile, []byte("# Outside secret file"), 0644)

	symlinkFile := filepath.Join(tempDir, "symlink.md")
	_ = os.Symlink(outsideFile, symlinkFile)

	res, err := Scan(ScanOptions{
		RootPath:          tempDir,
		AllowedExtensions: []string{".md"},
	})
	if err != nil {
		t.Fatalf("Scan failed: %v", err)
	}

	// Only valid.md should be discovered (symlink and hidden dir skipped)
	if res.TotalFiles != 1 {
		t.Fatalf("expected exactly 1 discovered file, got %d", res.TotalFiles)
	}
	if res.Batches[0].Files[0].Filename != "valid.md" {
		t.Errorf("expected valid.md, got %s", res.Batches[0].Files[0].Filename)
	}
}

func TestLoadFileContent(t *testing.T) {
	tempDir := t.TempDir()

	// Text file
	txtFile := filepath.Join(tempDir, "test.txt")
	txtData := []byte("Hello UTF-8 text")
	_ = os.WriteFile(txtFile, txtData, 0644)
	c1, err := LoadFileContent(txtFile, 1024*1024)
	if err != nil || !c1.IsText || c1.TextContent != "Hello UTF-8 text" {
		t.Errorf("unexpected LoadFileContent on text file: %+v, err=%v", c1, err)
	}
	expectedHash := sha256.Sum256(txtData)
	if c1.SHA256 != hex.EncodeToString(expectedHash[:]) {
		t.Errorf("expected hash %s, got %s", hex.EncodeToString(expectedHash[:]), c1.SHA256)
	}

	// Empty file
	emptyFile := filepath.Join(tempDir, "empty.txt")
	_ = os.WriteFile(emptyFile, []byte(""), 0644)
	c2, err := LoadFileContent(emptyFile, 1024*1024)
	if err != nil || !c2.IsText || c2.TextContent != "" {
		t.Errorf("unexpected LoadFileContent on empty file: %+v, err=%v", c2, err)
	}

	// Binary file with NUL byte
	binFile := filepath.Join(tempDir, "bin.dat")
	binData := []byte{0x00, 0xFF, 0xFE, 0x12}
	_ = os.WriteFile(binFile, binData, 0644)
	c3, err := LoadFileContent(binFile, 1024*1024)
	if err != nil || c3.IsText || c3.Base64Data == "" {
		t.Errorf("unexpected LoadFileContent on binary file: %+v, err=%v", c3, err)
	}

	// Oversized file rejection
	oversizedFile := filepath.Join(tempDir, "large.txt")
	_ = os.WriteFile(oversizedFile, make([]byte, 2048), 0644)
	_, err = LoadFileContent(oversizedFile, 1024)
	if err == nil {
		t.Errorf("expected error for oversized file loading, got nil")
	}

	// Symlink rejection on load
	symlink := filepath.Join(tempDir, "link_to_txt.txt")
	_ = os.Symlink(txtFile, symlink)
	_, err = LoadFileContent(symlink, 1024*1024)
	if err == nil {
		t.Errorf("expected error when loading symlink, got nil")
	}
}

func TestScanOversizedFile(t *testing.T) {
	tempDir := t.TempDir()
	largeFile := filepath.Join(tempDir, "huge.txt")
	_ = os.WriteFile(largeFile, make([]byte, 5000), 0644)

	_, err := Scan(ScanOptions{
		RootPath:     tempDir,
		MaxFileBytes: 1000,
	})
	if err == nil {
		t.Errorf("expected error during scan of oversized file, got nil")
	}
}

func TestScannerPathAwareDeduplication(t *testing.T) {
	tempDir := t.TempDir()

	// Create two distinct files with IDENTICAL content (same SHA-256)
	sharedContent := []byte("# Same Content Across Multiple Files")
	h := sha256.Sum256(sharedContent)
	expectedSHA := hex.EncodeToString(h[:])

	f1 := filepath.Join(tempDir, "path_a.md")
	f2 := filepath.Join(tempDir, "path_b.md")
	_ = os.WriteFile(f1, sharedContent, 0644)
	_ = os.WriteFile(f2, sharedContent, 0644)

	// Catalog only has path_a.md indexed
	knownDocs := map[string]string{
		"path_a.md": expectedSHA,
	}

	res, err := Scan(ScanOptions{
		RootPath:       tempDir,
		KnownDocuments: knownDocs,
	})
	if err != nil {
		t.Fatalf("Scan failed: %v", err)
	}

	// path_a.md must be skipped (known at that exact path)
	// path_b.md must NOT be skipped despite having the same SHA-256 (not known at path_b.md)
	if res.TotalFiles != 1 {
		t.Fatalf("expected exactly 1 file (path_b.md) to be discovered, got %d", res.TotalFiles)
	}
	if res.Batches[0].Files[0].RelPath != "path_b.md" {
		t.Errorf("expected path_b.md to be discovered, got %s", res.Batches[0].Files[0].RelPath)
	}
	if res.SkippedCount != 1 || res.SkippedFiles[0] != "path_a.md" {
		t.Errorf("expected path_a.md to be skipped, got count=%d, skipped=%v", res.SkippedCount, res.SkippedFiles)
	}
}
