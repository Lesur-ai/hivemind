package ingest

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/scanner"
)

// ProgressCallback is invoked during scanning and ingestion for TUI/CLI updates
type ProgressCallback func(stage string, message string, current int, total int, file string, jobID string, status string, errStr string)

// IngestOptions configures an ingestion run
type IngestOptions struct {
	Path                 string
	SpaceID              string
	Ontology             string
	BatchSizeMB          int
	MaxFileBytes         int64
	AllowedExtensions    []string
	DryRun               bool
	ForceReplace         bool
	CreateSpaceIfMissing bool
	RulesTemplate        string
	WatchJobs            bool
	Timeout              time.Duration
	NonInteractive       bool
}

// IngestResult captures the overall outcome of an ingestion execution
type IngestResult struct {
	SpaceID           string       `json:"space_id"`
	TotalScanned      int          `json:"total_scanned"`
	TotalUploaded     int          `json:"total_uploaded"`
	TotalSkipped      int          `json:"total_skipped"`
	TotalSucceeded    int          `json:"total_succeeded"`
	TotalPending      int          `json:"total_pending"`
	TotalFailed       int          `json:"total_failed"`
	TotalNotSubmitted int          `json:"total_not_submitted"`
	AutomaticSources  []string     `json:"automatic_sources,omitempty"`
	BatchesCount      int          `json:"batches_count"`
	DurationMs        int64        `json:"duration_ms"`
	Jobs              []*JobRecord `json:"jobs"`
	Success           bool         `json:"success"`
}

// JobRecord represents the status of an ingested file/job
type JobRecord struct {
	JobID    string `json:"job_id"`
	Filename string `json:"filename"`
	SHA256   string `json:"sha256"`
	Status   string `json:"status"` // queued, running, succeeded, failed, skipped
	Error    string `json:"error,omitempty"`
}

// Engine coordinates scanning, space verification, batching, and async ingestion
type Engine struct {
	client       *mcpclient.Client
	PollInterval time.Duration
}

// NewEngine creates a new Engine instance
func NewEngine(client *mcpclient.Client) *Engine {
	return &Engine{client: client}
}

// Run executes the ingestion workflow
func (e *Engine) Run(ctx context.Context, opts IngestOptions, callback ProgressCallback) (*IngestResult, error) {
	start := time.Now()
	automatic := strings.TrimSpace(opts.Ontology) == "auto"
	if automatic && !opts.WatchJobs && !opts.DryRun {
		return nil, fmt.Errorf("automatic ontology requires --watch; --no-poll/--watch=false cannot complete bootstrap before corpus ingestion")
	}
	// The installed server accepts 50 MiB documents and a 75 MiB JSON envelope.
	// Bound source bytes before loading/base64 encoding; CallTool checks exact JSON.
	configuredBatchSizeMB := opts.BatchSizeMB
	if opts.BatchSizeMB <= 0 || opts.BatchSizeMB > 50 {
		opts.BatchSizeMB = 50
	}
	if opts.MaxFileBytes <= 0 || opts.MaxFileBytes > 50*1024*1024 {
		opts.MaxFileBytes = int64(opts.BatchSizeMB) * 1024 * 1024
	}
	res := &IngestResult{
		SpaceID: opts.SpaceID,
		Jobs:    make([]*JobRecord, 0),
	}

	report := func(stage, msg string, current, total int, file, jobID, status, errStr string) {
		if callback != nil {
			callback(stage, msg, current, total, file, jobID, status, errStr)
		}
	}
	if configuredBatchSizeMB > 50 {
		report("configuration", fmt.Sprintf("Configured batch size %d MiB exceeds the server limit; using 50 MiB.", configuredBatchSizeMB), 0, 0, "", "", "", "")
	}

	// 1. Dry-run is 100% offline and local
	if opts.DryRun {
		report("scanning", fmt.Sprintf("Scanning %s locally (dry-run mode)...", opts.Path), 0, 0, "", "", "", "")
		scanRes, err := scanner.Scan(scanner.ScanOptions{
			RootPath:          opts.Path,
			AllowedExtensions: opts.AllowedExtensions,
			BatchSizeMB:       opts.BatchSizeMB,
			MaxFileBytes:      opts.MaxFileBytes,
			ForceReplace:      opts.ForceReplace,
		})
		if err != nil {
			return nil, fmt.Errorf("scanning failed: %w", err)
		}

		if automatic && scanRes.TotalFiles > 0 {
			selected, err := planAutomatic(scanRes, nil, int64(opts.BatchSizeMB)*1024*1024, opts.ForceReplace)
			if err != nil {
				return nil, err
			}
			for _, file := range selected {
				res.AutomaticSources = append(res.AutomaticSources, file.RelPath)
			}
		}
		res.TotalScanned = scanRes.TotalFiles + scanRes.SkippedCount
		res.TotalSkipped = scanRes.SkippedCount
		res.BatchesCount = len(scanRes.Batches)

		for _, skippedPath := range scanRes.SkippedFiles {
			res.Jobs = append(res.Jobs, &JobRecord{
				Filename: skippedPath,
				Status:   "skipped",
			})
		}
		for _, b := range scanRes.Batches {
			for _, f := range b.Files {
				res.Jobs = append(res.Jobs, &JobRecord{
					Filename: f.RelPath,
					SHA256:   f.SHA256,
					Status:   "dry_run",
				})
			}
		}

		res.Success = true
		res.DurationMs = time.Since(start).Milliseconds()
		report("dry_run", fmt.Sprintf("Dry-run complete. Discovered %d files in %d batches.", scanRes.TotalFiles, len(scanRes.Batches)), scanRes.TotalFiles, scanRes.TotalFiles, "", "", "dry_run", "")
		return res, nil
	}

	// 2. Ensure space exists or auto-create if requested
	report("verifying_space", fmt.Sprintf("Verifying space '%s'...", opts.SpaceID), 0, 0, "", "", "", "")
	if err := e.EnsureSpace(ctx, opts.SpaceID, opts.CreateSpaceIfMissing, opts.RulesTemplate); err != nil {
		return nil, fmt.Errorf("space verification failed: %w", err)
	}

	// 3. Inspect active running jobs in space to notify operator
	activeJobs, _ := e.FetchActiveJobs(ctx, opts.SpaceID)
	if activeJobs > 0 {
		report("active_jobs", fmt.Sprintf("Notice: %d active ingestion job(s) in space '%s'.", activeJobs, opts.SpaceID), 0, 0, "", "", "", "")
	}

	// 4. Fetch known documents for path-aware deduplication (unless force replace is specified)
	var knownDocs map[string]string
	if !opts.ForceReplace {
		report("fetching_hashes", "Checking remote document catalog for deduplication...", 0, 0, "", "", "", "")
		docs, err := e.FetchKnownDocuments(ctx, opts.SpaceID)
		if err != nil {
			return nil, fmt.Errorf("failed to fetch remote document catalog: %w", err)
		}
		if len(docs) > 0 {
			knownDocs = docs
			report("hashes_loaded", fmt.Sprintf("Loaded %d existing indexed document(s).", len(docs)), 0, 0, "", "", "", "")
		}
	}

	// 5. Automatic bootstrap selection must include already-successful sources.
	scanKnown := knownDocs
	if automatic {
		scanKnown = nil
	}
	// Scan directory
	report("scanning", fmt.Sprintf("Scanning %s...", opts.Path), 0, 0, "", "", "", "")
	scanRes, err := scanner.Scan(scanner.ScanOptions{
		RootPath:          opts.Path,
		AllowedExtensions: opts.AllowedExtensions,
		BatchSizeMB:       opts.BatchSizeMB,
		MaxFileBytes:      opts.MaxFileBytes,
		KnownDocuments:    scanKnown,
		ForceReplace:      opts.ForceReplace,
	})
	if err != nil {
		return nil, fmt.Errorf("scanning failed: %w", err)
	}

	if automatic && scanRes.TotalFiles > 0 {
		selected, err := planAutomatic(scanRes, knownDocs, int64(opts.BatchSizeMB)*1024*1024, opts.ForceReplace)
		if err != nil {
			return nil, err
		}
		for _, file := range selected {
			res.AutomaticSources = append(res.AutomaticSources, file.RelPath)
		}
		report("automatic_sources", fmt.Sprintf("Automatic bootstrap: %d complete sources selected from the full inventory.", len(selected)), 0, scanRes.TotalFiles, "", "", "", "")
	}
	res.TotalScanned = scanRes.TotalFiles + scanRes.SkippedCount
	res.TotalSkipped = scanRes.SkippedCount
	res.BatchesCount = len(scanRes.Batches)

	for _, skippedPath := range scanRes.SkippedFiles {
		res.Jobs = append(res.Jobs, &JobRecord{
			Filename: skippedPath,
			Status:   "skipped",
		})
	}

	report("scanned", fmt.Sprintf("Discovered %d new files to ingest (%d skipped as unchanged).", scanRes.TotalFiles, scanRes.SkippedCount), scanRes.TotalFiles, scanRes.TotalFiles, "", "", "", "")

	if scanRes.TotalFiles == 0 {
		res.Success = true
		res.DurationMs = time.Since(start).Milliseconds()
		report("done", "No new files to ingest. Everything is up to date.", 0, 0, "", "", "finished", "")
		return res, nil
	}

	// 6. Resolve and validate ontology schema if specified
	var resolvedOntologyYAML string
	if opts.Ontology != "" && !automatic {
		report("resolving_ontology", fmt.Sprintf("Resolving and validating ontology '%s'...", opts.Ontology), 0, 0, "", "", "", "")
		resolved, err := e.ResolveOntology(ctx, opts.SpaceID, opts.Ontology)
		if err != nil {
			return nil, fmt.Errorf("ontology resolution failed: %w", err)
		}
		resolvedOntologyYAML = resolved
	}

	// Each watched batch is drained before the next admission. No-poll remains
	// submit-only; it does not gain hidden waits or retries.
	totalProcessed := 0
	recordJobs := func(batchJobs []*JobRecord) {
		for _, jobRec := range batchJobs {
			totalProcessed++
			st := strings.ToLower(jobRec.Status)
			switch st {
			case "failed", "error", "queue_full", "cancelled", "rejected":
				jobRec.Status = "failed"
				res.TotalFailed++
				res.Jobs = append(res.Jobs, jobRec)
				report("upload_failed", fmt.Sprintf("Failed to upload %s: %s", jobRec.Filename, jobRec.Error), totalProcessed, scanRes.TotalFiles, jobRec.Filename, "", "failed", jobRec.Error)
			case "skipped", "changed_skipped":
				jobRec.Status = "skipped"
				res.TotalSkipped++
				res.Jobs = append(res.Jobs, jobRec)
				report("skipped", fmt.Sprintf("Skipped %s (%s)", jobRec.Filename, jobRec.Error), totalProcessed, scanRes.TotalFiles, jobRec.Filename, "", "skipped", "")
			case "succeeded":
				jobRec.Status = "succeeded"
				res.TotalSucceeded++
				res.TotalUploaded++
				res.Jobs = append(res.Jobs, jobRec)
				report("uploaded", fmt.Sprintf("Uploaded %s", jobRec.Filename), totalProcessed, scanRes.TotalFiles, jobRec.Filename, jobRec.JobID, "succeeded", "")
			case "queued", "running", "processing", "pending", "in_progress":
				if jobRec.JobID == "" {
					jobRec.Status = "failed"
					jobRec.Error = "server returned pending status without job_id"
					res.TotalFailed++
					res.Jobs = append(res.Jobs, jobRec)
					report("upload_failed", fmt.Sprintf("Failed %s: missing job_id", jobRec.Filename), totalProcessed, scanRes.TotalFiles, jobRec.Filename, "", "failed", jobRec.Error)
				} else {
					res.TotalUploaded++
					res.TotalPending++
					res.Jobs = append(res.Jobs, jobRec)
					report("uploaded", fmt.Sprintf("Uploaded %s (Job ID: %s)", jobRec.Filename, jobRec.JobID), totalProcessed, scanRes.TotalFiles, jobRec.Filename, jobRec.JobID, jobRec.Status, "")
				}
			default:
				jobRec.Status = "failed"
				jobRec.Error = fmt.Sprintf("unrecognized job status '%s'", jobRec.Status)
				res.TotalFailed++
				res.Jobs = append(res.Jobs, jobRec)
				report("upload_failed", fmt.Sprintf("Failed %s: %s", jobRec.Filename, jobRec.Error), totalProcessed, scanRes.TotalFiles, jobRec.Filename, "", "failed", jobRec.Error)
			}
		}
	}
	watchJobs := func(batchJobs []*JobRecord) {
		if opts.WatchJobs && len(batchJobs) > 0 {
			report("watching", "Monitoring background ingestion jobs...", 0, len(batchJobs), "", "", "", "")
			for i, job := range batchJobs {
				if job.Status == "skipped" || job.Status == "failed" || job.JobID == "" || job.Status == "succeeded" {
					continue
				}

				report("job_polling", fmt.Sprintf("Checking status for job %s (%s)...", job.JobID, job.Filename), i+1, len(batchJobs), job.Filename, job.JobID, "polling", "")
				status, err := e.PollJob(ctx, opts.SpaceID, job.JobID, opts.Timeout)
				res.TotalPending--
				if err != nil {
					job.Status = "failed"
					job.Error = err.Error()
					res.TotalFailed++
					report("job_failed", fmt.Sprintf("Job %s failed: %v", job.JobID, err), i+1, len(batchJobs), job.Filename, job.JobID, "failed", err.Error())
				} else {
					job.Status = status
					switch status {
					case "succeeded":
						res.TotalSucceeded++
						report("job_succeeded", fmt.Sprintf("Job %s succeeded (%s).", job.JobID, job.Filename), i+1, len(batchJobs), job.Filename, job.JobID, "succeeded", "")
					case "skipped", "changed_skipped":
						res.TotalSkipped++
						report("skipped", fmt.Sprintf("Job %s skipped (%s).", job.JobID, job.Filename), i+1, len(batchJobs), job.Filename, job.JobID, "skipped", "")
					default:
						res.TotalFailed++
						report("job_failed", fmt.Sprintf("Job %s ended with status: %s", job.JobID, status), i+1, len(batchJobs), job.Filename, job.JobID, status, "")
					}
				}
			}
		}
	}
batchLoop:
	for batchIdx, batch := range scanRes.Batches {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		report("batch_starting", fmt.Sprintf("Processing batch [%d/%d] (%d files)...", batchIdx+1, len(scanRes.Batches), len(batch.Files)), totalProcessed, scanRes.TotalFiles, "", "", "uploading", "")
		ontology := resolvedOntologyYAML
		bootstrap := automatic && batchIdx == 0
		if bootstrap {
			ontology = "auto"
		}
		current := batch
		for attempt := 0; ; attempt++ {
			batchJobs, err := e.IngestBatch(ctx, opts.SpaceID, current, ontology, opts.MaxFileBytes, opts.ForceReplace)
			if err != nil {
				batchJobs = nil
				for _, file := range current.Files {
					batchJobs = append(batchJobs, &JobRecord{Filename: file.RelPath, SHA256: file.SHA256, Status: "failed", Error: err.Error()})
				}
			}
			var admitted []*JobRecord
			retry := scanner.Batch{}
			for _, job := range batchJobs {
				// Only an explicit queue_full without a job ID is safe to resubmit.
				// Never replay an admitted document or an ambiguous transport/auth failure.
				if job.Status == "queue_full" && job.JobID == "" && opts.WatchJobs && !bootstrap && attempt < 3 {
					matched := false
					for _, file := range current.Files {
						if file.RelPath == job.Filename {
							matched = true
							retry.Files = append(retry.Files, file)
							retry.TotalSize += file.Size
							break
						}
					}
					if matched {
						continue
					}
				}
				admitted = append(admitted, job)
			}
			recordJobs(admitted)
			watchJobs(admitted)
			if bootstrap && (res.TotalFailed > 0 || res.TotalPending > 0) {
				for _, rest := range scanRes.Batches[batchIdx+1:] {
					for _, file := range rest.Files {
						res.TotalNotSubmitted++
						res.Jobs = append(res.Jobs, &JobRecord{Filename: file.RelPath, SHA256: file.SHA256, Status: "not_submitted", Error: "Automatic bootstrap did not succeed; resubmit the unchanged corpus to resume."})
					}
				}
				break batchLoop
			}
			if len(retry.Files) == 0 {
				break
			}
			delay := time.Second
			if e.PollInterval > 0 {
				delay = e.PollInterval
			}
			delay *= time.Duration(1 << attempt)
			report("queue_retry", fmt.Sprintf("Queue full: retrying %d refused documents after %s.", len(retry.Files), delay), totalProcessed, scanRes.TotalFiles, "", "", "queue_full", "")
			timer := time.NewTimer(delay)
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-timer.C:
			}
			current = retry
		}
	}

	res.Success = (res.TotalFailed == 0 && res.TotalPending == 0 && (res.TotalSucceeded+res.TotalSkipped) == res.TotalScanned)
	res.DurationMs = time.Since(start).Milliseconds()
	if res.TotalNotSubmitted > 0 {
		report("stopped", fmt.Sprintf("Bootstrap failed; %d documents not submitted.", res.TotalNotSubmitted), totalProcessed, res.TotalScanned, "", "", "failed", "")
	} else if res.TotalPending > 0 {
		report("submitted", fmt.Sprintf("Submitted ingestion in %d ms (%d succeeded, %d pending, %d failed).", res.DurationMs, res.TotalSucceeded, res.TotalPending, res.TotalFailed), res.TotalScanned, res.TotalScanned, "", "", "pending", "")
	} else {
		report("done", fmt.Sprintf("Ingestion completed in %d ms (%d succeeded, %d failed).", res.DurationMs, res.TotalSucceeded, res.TotalFailed), res.TotalScanned, res.TotalScanned, "", "", "finished", "")
	}
	return res, nil
}

// FetchActiveJobs inspects running and queued ingestion jobs on the space
func (e *Engine) FetchActiveJobs(ctx context.Context, spaceID string) (int, error) {
	res, err := e.client.CallTool(ctx, "long_ingest_list", map[string]interface{}{
		"space_id": spaceID,
		"status":   "running",
		"limit":    50,
	})
	if err != nil || res["status"] == "error" || res["isError"] == true {
		return 0, nil
	}
	jobs, ok := res["jobs"].([]interface{})
	if !ok {
		return 0, nil
	}
	return len(jobs), nil
}

// EnsureSpace checks if space exists, creating it only on explicit not-found
func (e *Engine) EnsureSpace(ctx context.Context, spaceID string, createIfMissing bool, rulesTemplate string) error {
	res, err := e.client.CallTool(ctx, "space_info", map[string]interface{}{"space_id": spaceID})
	if err == nil {
		if status, ok := res["status"].(string); ok && status == "ok" {
			return nil
		}
		if status, ok := res["status"].(string); ok && status == "not_found" {
			if !createIfMissing {
				return fmt.Errorf("space %s does not exist (use --create-space-if-missing to create it automatically)", spaceID)
			}
			var rulesParam string
			if rulesTemplate == "" || rulesTemplate == "standard" {
				rulesParam = ""
			} else if fileInfo, err := os.Stat(rulesTemplate); err == nil && fileInfo.Mode().IsRegular() {
				if !strings.HasSuffix(strings.ToLower(rulesTemplate), ".md") {
					return fmt.Errorf("rules file '%s' must have a .md extension", rulesTemplate)
				}
				data, err := os.ReadFile(rulesTemplate)
				if err != nil {
					return fmt.Errorf("failed to read rules template file %s: %w", rulesTemplate, err)
				}
				rulesParam = string(data)
			} else if strings.Contains(rulesTemplate, "\n") || strings.HasPrefix(rulesTemplate, "#") {
				rulesParam = rulesTemplate
			} else {
				return fmt.Errorf("unknown rules template '%s' (supported: 'standard', or provide path to a .md rules file)", rulesTemplate)
			}
			createRes, err := e.client.CallTool(ctx, "space_create", map[string]interface{}{
				"space_id":    spaceID,
				"description": fmt.Sprintf("Space for %s created by hivemind-ingest", spaceID),
				"rules":       rulesParam,
			})
			if err != nil {
				return fmt.Errorf("space_create failed: %w", err)
			}
			createStatus, _ := createRes["status"].(string)
			if createStatus != "created" && createStatus != "already_exists" {
				return fmt.Errorf("space_create failed with status '%s': %v", createStatus, createRes["message"])
			}
			return nil
		}
		return fmt.Errorf("space verification returned status '%v': %v", res["status"], res["message"])
	}
	return fmt.Errorf("failed to verify space %s: %w", spaceID, err)
}

// FetchKnownDocuments retrieves the map of source_path -> SHA256 hashes for successfully indexed documents
func (e *Engine) FetchKnownDocuments(ctx context.Context, spaceID string) (map[string]string, error) {
	known := make(map[string]string)
	limit := 100
	offset := 0

	for {
		// Attempt to list documents using long_document_list (Issue #464)
		res, err := e.client.CallTool(ctx, "long_document_list", map[string]interface{}{
			"space_id": spaceID,
			"limit":    limit,
			"offset":   offset,
		})
		if err != nil {
			var jErr *mcpclient.JSONRPCError
			if errors.As(err, &jErr) && jErr.Code == -32601 {
				// Method not found: remote document catalog is not yet exposed by server (Issue #464)
				return known, nil
			}
			errStr := strings.ToLower(err.Error())
			if strings.Contains(errStr, "unknown tool: long_document_list") || strings.Contains(errStr, "tool 'long_document_list' not found") {
				return known, nil
			}
			return nil, fmt.Errorf("failed to fetch document catalog for space %s: %w", spaceID, err)
		}
		if res["status"] == "error" || res["isError"] == true {
			msg, _ := res["message"].(string)
			msgLower := strings.ToLower(msg)
			if strings.Contains(msgLower, "unknown tool: long_document_list") || strings.Contains(msgLower, "tool 'long_document_list' not found") {
				return known, nil
			}
			// Catalog reads do not provision embedded bindings. A fresh embedded
			// space can be empty only when long_status positively confirms it.
			unbound := fmt.Sprintf("Space '%s' is not connected to Graph Memory. Use graph_connect first.", spaceID)
			if offset == 0 && msg == unbound {
				status, statusErr := e.client.CallTool(ctx, "long_status", map[string]interface{}{"space_id": spaceID})
				if statusErr == nil && isUnboundEmbeddedStatus(status, spaceID) {
					return known, nil
				}
			}
			return nil, fmt.Errorf("server error from long_document_list: %s", msg)
		}

		rawDocs, ok := res["documents"].([]interface{})
		if !ok {
			return nil, fmt.Errorf("invalid or missing 'documents' array in space %s catalog response", spaceID)
		}
		if len(rawDocs) == 0 {
			break
		}

		for idx, item := range rawDocs {
			docMap, ok := item.(map[string]interface{})
			if !ok || docMap == nil {
				return nil, fmt.Errorf("malformed document entry at index %d in space %s catalog", idx, spaceID)
			}
			sourcePath, _ := docMap["source_path"].(string)
			if sourcePath == "" {
				// Ignore pathless / legacy entries for path-aware deduplication
				continue
			}

			// Only successfully ingested / indexed documents are considered known for skip deduplication.
			// Failed, pending, error or absent states must be retried.
			ingestionStatus, _ := docMap["ingestion_status"].(string)
			if ingestionStatus == "" {
				ingestionStatus, _ = docMap["status"].(string)
			}
			stLower := strings.ToLower(ingestionStatus)
			if stLower != "completed" && stLower != "success" && stLower != "succeeded" && stLower != "indexed" && stLower != "ok" {
				// Incomplete status: eligible for retry, skip deduplication
				continue
			}

			sha, ok := docMap["sha256"].(string)
			if !ok || len(sha) != 64 || !isValidHex(sha) {
				return nil, fmt.Errorf("invalid sha256 checksum '%v' for document %s at index %d in space %s catalog", docMap["sha256"], sourcePath, idx, spaceID)
			}
			known[sourcePath] = strings.ToLower(sha)
		}

		if len(rawDocs) < limit {
			break
		}
		offset += len(rawDocs)
	}

	return known, nil
}

// isUnboundEmbeddedStatus requires the exact positive first-ingest contract;
// missing fields, remote bindings, and any error/recovery marker fail closed.
func isUnboundEmbeddedStatus(status map[string]interface{}, spaceID string) bool {
	if status["status"] != "ok" || status["space_id"] != spaceID ||
		status["connected"] != false || status["bound"] != false || status["embedded"] != true {
		return false
	}
	if value, present := status["isError"]; present && value != false {
		return false
	}
	for _, key := range []string{"error", "code", "recovery_required"} {
		if _, present := status[key]; present {
			return false
		}
	}
	return true
}

// FetchKnownHashes retrieves the list of SHA256 hashes already indexed in the space (deprecated)
func (e *Engine) FetchKnownHashes(ctx context.Context, spaceID string) (map[string]bool, error) {
	docs, err := e.FetchKnownDocuments(ctx, spaceID)
	if err != nil {
		return nil, err
	}
	hashes := make(map[string]bool, len(docs))
	for _, sha := range docs {
		hashes[sha] = true
	}
	return hashes, nil
}

func isValidHex(s string) bool {
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return true
}

// IngestBatch streams an entire batch of documents in a single long_ingest_async call
func (e *Engine) IngestBatch(ctx context.Context, spaceID string, batch scanner.Batch, ontology string, maxBytes int64, replace bool) ([]*JobRecord, error) {
	if len(batch.Files) == 0 {
		return nil, nil
	}

	docsPayload := make([]map[string]interface{}, 0, len(batch.Files))
	fileMap := make(map[string]scanner.FileItem)

	for _, file := range batch.Files {
		contentObj, err := scanner.LoadFileContent(file.Path, maxBytes)
		if err != nil {
			return nil, fmt.Errorf("failed to read file %s: %w", file.Path, err)
		}

		if ontology == "auto" && contentObj.SHA256 != file.SHA256 {
			return nil, fmt.Errorf("automatic source %s changed since scan; rerun with unchanged sources", file.RelPath)
		}
		// Graph Memory backend strictly requires content_base64, filename, source_path, and sha256
		doc := map[string]interface{}{
			"source_path":    file.RelPath,
			"filename":       file.Filename,
			"sha256":         contentObj.SHA256,
			"content_base64": contentObj.Base64Data,
			"metadata": map[string]interface{}{
				"content_type": file.ContentType,
			},
		}

		docsPayload = append(docsPayload, doc)
		fileMap[file.RelPath] = file
	}

	options := map[string]interface{}{
		"replace_existing": replace,
	}
	if ontology != "" {
		options["ontology"] = ontology
	}

	res, err := e.client.CallTool(ctx, "long_ingest_async", map[string]interface{}{
		"space_id":  spaceID,
		"documents": docsPayload,
		"options":   options,
	})
	if err != nil {
		return nil, fmt.Errorf("long_ingest_async failed: %w", err)
	}

	if res["status"] == "error" || res["isError"] == true {
		msg, _ := res["message"].(string)
		if msg == "" {
			msg = "unknown error from long_ingest_async"
		}
		return nil, fmt.Errorf("%s", msg)
	}

	// Index any explicit error details from errors[]
	errorMap := make(map[string]string)
	if errorsList, ok := res["errors"].([]interface{}); ok {
		for _, errRaw := range errorsList {
			if errMap, ok := errRaw.(map[string]interface{}); ok {
				sp, _ := errMap["source_path"].(string)
				if sp == "" {
					sp, _ = errMap["filename"].(string)
				}
				errStr, _ := errMap["error"].(string)
				if sp != "" && errStr != "" {
					errorMap[sp] = errStr
				}
			}
		}
	}

	records := make([]*JobRecord, 0, len(batch.Files))
	handledFiles := make(map[string]bool)

	// 1. Process items array (canonical response from memory_ingest_batch_async)
	if itemsList, ok := res["items"].([]interface{}); ok && len(itemsList) > 0 {
		for _, itRaw := range itemsList {
			if itMap, ok := itRaw.(map[string]interface{}); ok {
				sourcePath, _ := itMap["source_path"].(string)
				if sourcePath == "" {
					sourcePath, _ = itMap["filename"].(string)
				}
				jobID, _ := itMap["job_id"].(string)
				itemStatus, _ := itMap["status"].(string)
				itemMsg, _ := itMap["message"].(string)
				if itemStatus == "" {
					itemStatus = "queued"
				}
				fileItem := fileMap[sourcePath]
				rec := &JobRecord{
					JobID:    jobID,
					Filename: sourcePath,
					SHA256:   fileItem.SHA256,
					Status:   itemStatus,
				}
				stLower := strings.ToLower(itemStatus)
				if stLower == "error" || stLower == "failed" || stLower == "queue_full" || stLower == "cancelled" || stLower == "rejected" {
					if itemMsg == "" && errorMap[sourcePath] != "" {
						itemMsg = errorMap[sourcePath]
					}
					if itemMsg == "" {
						itemMsg = fmt.Sprintf("batch item status: %s", itemStatus)
					}
					rec.Error = itemMsg
				}
				records = append(records, rec)
				handledFiles[sourcePath] = true
			}
		}
	} else if jobsList, ok := res["jobs"].([]interface{}); ok && len(jobsList) > 0 {
		// 2. Compatibility fallback with older jobs[] envelope
		for _, jRaw := range jobsList {
			if jMap, ok := jRaw.(map[string]interface{}); ok {
				jobID, _ := jMap["job_id"].(string)
				filename, _ := jMap["filename"].(string)
				sourcePath, _ := jMap["source_path"].(string)
				status, _ := jMap["status"].(string)
				if status == "" {
					status = "queued"
				}
				key := sourcePath
				if key == "" {
					key = filename
				}
				fileItem := fileMap[key]
				rec := &JobRecord{
					JobID:    jobID,
					Filename: key,
					SHA256:   fileItem.SHA256,
					Status:   status,
				}
				stLower := strings.ToLower(status)
				if stLower == "error" || stLower == "failed" || stLower == "queue_full" {
					if errorMap[key] != "" {
						rec.Error = errorMap[key]
					}
				}
				records = append(records, rec)
				handledFiles[key] = true
			}
		}
	} else if singleJobID, ok := res["job_id"].(string); ok && singleJobID != "" {
		// 3. Single job_id for single-document ingestion
		st, _ := res["status"].(string)
		if st == "" {
			st = "queued"
		}
		for _, file := range batch.Files {
			records = append(records, &JobRecord{
				JobID:    singleJobID,
				Filename: file.RelPath,
				SHA256:   file.SHA256,
				Status:   st,
			})
			handledFiles[file.RelPath] = true
		}
	} else {
		return nil, fmt.Errorf("backend returned invalid batch response: missing items/job_id list (got status=%v)", res["status"])
	}

	// 4. Ensure bijection: all submitted files must be accounted for
	for _, file := range batch.Files {
		if !handledFiles[file.RelPath] {
			errStr := errorMap[file.RelPath]
			if errStr == "" {
				errStr = errorMap[file.Filename]
			}
			if errStr == "" {
				errStr = "file unacknowledged in batch response"
			}
			records = append(records, &JobRecord{
				Filename: file.RelPath,
				SHA256:   file.SHA256,
				Status:   "failed",
				Error:    errStr,
			})
			handledFiles[file.RelPath] = true
		}
	}

	return records, nil
}

// IngestFile streams a single file (used for single-file operations)
func (e *Engine) IngestFile(ctx context.Context, spaceID string, file scanner.FileItem, ontology string, maxBytes int64, replace bool) (*JobRecord, error) {
	batch := scanner.Batch{
		Files: []scanner.FileItem{file},
	}
	jobs, err := e.IngestBatch(ctx, spaceID, batch, ontology, maxBytes, replace)
	if err != nil {
		return nil, err
	}
	if len(jobs) == 0 {
		return nil, fmt.Errorf("no job returned for file %s", file.RelPath)
	}
	return jobs[0], nil
}

// PollJob monitors job progress until completion or context cancellation
func (e *Engine) PollJob(ctx context.Context, spaceID string, jobID string, timeout time.Duration) (string, error) {
	if timeout <= 0 {
		timeout = 10 * time.Minute
	}

	pollCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	interval := 1 * time.Second
	if e.PollInterval > 0 {
		interval = e.PollInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-pollCtx.Done():
			return "cancelled", pollCtx.Err()
		case <-ticker.C:
			res, err := e.client.CallTool(pollCtx, "long_ingest_status", map[string]interface{}{
				"space_id": spaceID,
				"job_id":   jobID,
			})
			if err != nil {
				// Fallback to long_ingest_job_status if needed
				res, err = e.client.CallTool(pollCtx, "long_ingest_job_status", map[string]interface{}{
					"space_id": spaceID,
					"job_id":   jobID,
				})
				if err != nil {
					return "failed", err
				}
			}

			if isErr, _ := res["isError"].(bool); isErr {
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = "status lookup returned MCP error"
				}
				return "failed", fmt.Errorf("%s", errMsg)
			}
			topStatus, _ := res["status"].(string)
			if strings.EqualFold(strings.TrimSpace(topStatus), "error") {
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = "status lookup returned error envelope"
				}
				return "failed", fmt.Errorf("%s", errMsg)
			}
			status := topStatus

			switch strings.ToLower(strings.TrimSpace(status)) {
			case "succeeded":
				return "succeeded", nil
			case "skipped", "changed_skipped":
				return "skipped", nil
			case "failed", "cancelled":
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = fmt.Sprintf("ingestion job ended with status %s", status)
				}
				return "failed", fmt.Errorf("%s", errMsg)
			case "queued", "running", "in_progress", "processing", "pending":
				// continue polling
			default:
				return "failed", fmt.Errorf("pollJob received non-job/unrecognized status %q", status)
			}
		}
	}
}

// ResolveOntology resolves a schema name, local YAML file, or raw YAML string into valid YAML content
func (e *Engine) ResolveOntology(ctx context.Context, spaceID, ontologyInput string) (string, error) {
	if ontologyInput == "" {
		return "", nil
	}

	var ontologyYAML string
	cleanPath := filepath.Clean(ontologyInput)
	if strings.HasSuffix(strings.ToLower(cleanPath), ".yaml") || strings.HasSuffix(strings.ToLower(cleanPath), ".yml") || fileExists(cleanPath) {
		content, err := os.ReadFile(cleanPath)
		if err != nil {
			return "", fmt.Errorf("failed to read ontology YAML file %s: %w", cleanPath, err)
		}
		ontologyYAML = string(content)
	} else if strings.Contains(ontologyInput, "\n") && (strings.Contains(ontologyInput, ":") || strings.HasPrefix(ontologyInput, "name:")) {
		ontologyYAML = ontologyInput
	} else {
		// Resolve named ontology via ontology_get
		getRes, err := e.client.CallTool(ctx, "ontology_get", map[string]interface{}{
			"space_id": spaceID,
			"name":     ontologyInput,
		})
		if err != nil {
			return "", fmt.Errorf("failed to resolve named ontology %s: %w", ontologyInput, err)
		}
		if getRes["status"] == "error" || getRes["isError"] == true {
			return "", fmt.Errorf("failed to fetch ontology %s: %v", ontologyInput, getRes["message"])
		}
		if cYAML, ok := getRes["content_yaml"].(string); ok && cYAML != "" {
			ontologyYAML = cYAML
		} else if cYAML, ok := getRes["content"].(string); ok && cYAML != "" {
			ontologyYAML = cYAML
		} else if cYAML, ok := getRes["yaml"].(string); ok && cYAML != "" {
			ontologyYAML = cYAML
		} else {
			return "", fmt.Errorf("ontology %s returned empty schema content", ontologyInput)
		}
	}

	// Validate ontology YAML with ontology_validate
	valRes, err := e.client.CallTool(ctx, "ontology_validate", map[string]interface{}{
		"space_id":     spaceID,
		"content_yaml": ontologyYAML,
	})
	if err != nil {
		return "", fmt.Errorf("failed to validate ontology %s: %w", ontologyInput, err)
	}
	if valRes["status"] == "error" || valRes["isError"] == true {
		return "", fmt.Errorf("ontology validation error: %v", valRes["message"])
	}
	if isValid, ok := valRes["valid"].(bool); ok && !isValid {
		return "", fmt.Errorf("ontology %s is structurally or semantically invalid: %+v", ontologyInput, valRes["errors"])
	}

	return ontologyYAML, nil
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	return !info.IsDir()
}
