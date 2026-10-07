package ingest

import (
	"crypto/sha256"
	"fmt"
	"path/filepath"
	"strings"

	"hivemind-ingest/internal/scanner"
)

// planAutomatic selects whole files from the complete inventory, before any
// successful-document filtering. An unchanged corpus yields the same bootstrap
// after restart. The existing server performs D1/D2 over these full sources.
func planAutomatic(scan *scanner.ScanResult, known map[string]string, maxBytes int64, replace bool) ([]scanner.FileItem, error) {
	var remaining []scanner.FileItem
	for _, batch := range scan.Batches {
		remaining = append(remaining, batch.Files...)
	}
	var selected []scanner.FileItem
	groups := map[string]bool{}
	hashes, dirs, extensions := map[string]bool{}, map[string]bool{}, map[string]bool{}
	selectedPaths := map[string]bool{}
	var total int64
	for len(selected) < 12 {
		best, bestScore, bestTie := -1, -1, ""
		for i, file := range remaining {
			if file.Size <= 0 || total+file.Size > maxBytes {
				continue
			}
			group := strings.Split(filepath.ToSlash(filepath.Dir(file.RelPath)), "/")[0]
			score := 0
			if !groups[group] {
				score += 2000
			}
			if !hashes[file.SHA256] {
				score += 1000
			}
			if !dirs[filepath.Dir(file.RelPath)] {
				score += 100
			}
			if !extensions[strings.ToLower(filepath.Ext(file.RelPath))] {
				score += 20
			}
			tie := fmt.Sprintf("%x", sha256.Sum256([]byte("hivemind-cli-auto-v1\x00"+file.RelPath+"\x00"+file.SHA256)))
			if best < 0 || score > bestScore || (score == bestScore && tie < bestTie) {
				best, bestScore, bestTie = i, score, tie
			}
		}
		if best < 0 {
			break
		}
		file := remaining[best]
		selected = append(selected, file)
		total += file.Size
		selectedPaths[file.RelPath] = true
		groups[strings.Split(filepath.ToSlash(filepath.Dir(file.RelPath)), "/")[0]] = true
		hashes[file.SHA256] = true
		dirs[filepath.Dir(file.RelPath)] = true
		extensions[strings.ToLower(filepath.Ext(file.RelPath))] = true
		remaining = append(remaining[:best], remaining[best+1:]...)
	}
	if len(selected) == 0 {
		return nil, fmt.Errorf("automatic ontology requires at least one non-empty source within the batch limit")
	}
	batches := []scanner.Batch{{Files: selected, TotalSize: total}}
	for _, batch := range scan.Batches {
		next := scanner.Batch{Index: len(batches)}
		for _, file := range batch.Files {
			if selectedPaths[file.RelPath] {
				continue
			}
			// Keep path/hash equality aligned with scanner.Scan's KnownDocuments rule.
			if !replace && strings.EqualFold(known[file.RelPath], file.SHA256) {
				scan.SkippedFiles = append(scan.SkippedFiles, file.RelPath)
				scan.SkippedCount++
				scan.TotalFiles--
				continue
			}
			next.Files = append(next.Files, file)
			next.TotalSize += file.Size
		}
		if len(next.Files) > 0 {
			batches = append(batches, next)
		}
	}
	scan.Batches = batches
	return selected, nil
}
