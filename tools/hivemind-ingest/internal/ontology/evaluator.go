package ontology

import (
	"context"
	"crypto/rand"
	"fmt"
	"math"
	"math/big"
	"os"
	"strings"
	"time"

	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/scanner"
)

// EvalOptions configures the ontology adequacy evaluation
type EvalOptions struct {
	RootPath           string        `json:"root_path"`
	SpaceID            string        `json:"space_id"`
	Ontology           string        `json:"ontology"`
	SampleSize         int           `json:"sample_size"`
	ThresholdOther     int           `json:"threshold_other"`
	KeepMemory         bool          `json:"keep_memory"`
	AllowExistingSpace bool          `json:"allow_existing_space"`
	AllowedExtensions  []string      `json:"allowed_extensions"`
	AllowedExts        []string      `json:"allowed_exts"` // alias
	MaxFileBytes       int64         `json:"max_file_bytes"`
	Timeout            time.Duration `json:"timeout"`
}

// UnclassifiedConcept holds information about an untyped entity
type UnclassifiedConcept struct {
	Name      string   `json:"name"`
	Frequency int      `json:"frequency,omitempty"`
	Sources   []string `json:"sources,omitempty"`
}

// EvalResult contains the complete adequacy assessment and actionable report
type EvalResult struct {
	OntologyPath         string                `json:"ontology_path"`
	SpaceID              string                `json:"space_id"`
	IsTemporarySpace     bool                  `json:"is_temporary_space"`
	SampleCount          int                   `json:"sample_count"`
	SampleFiles          []string              `json:"sample_files"`
	TotalEntities        int                   `json:"total_entities"`
	TypedEntities        int                   `json:"typed_entities"`
	OtherEntities        int                   `json:"other_entities"`
	OtherPercentage      float64               `json:"other_percentage"`
	RelevanceTier        string                `json:"relevance_tier"` // Excellent (<1%), Very Good (<5%), Moderate (<10%), Inadequate (>=10%)
	EntityTypesBreakdown map[string]int        `json:"entity_types_breakdown,omitempty"`
	UnclassifiedConcepts []UnclassifiedConcept `json:"unclassified_concepts,omitempty"`
	ThresholdOther       int                   `json:"threshold_other"`
	PassedThreshold      bool                  `json:"passed_threshold"`
	ExtractionStatus     string                `json:"extraction_status"`
	SpaceCleanupStatus   string                `json:"space_cleanup_status,omitempty"` // "cleaned", "retained", "failed"
	SpaceCleanupMessage  string                `json:"space_cleanup_message,omitempty"`
	Message              string                `json:"message"`
	Suggestions          []string              `json:"suggestions,omitempty"`
}

// Evaluator evaluates ontology fit against document samples
type Evaluator struct {
	client       *mcpclient.Client
	PollInterval time.Duration
}

// NewEvaluator creates a new Evaluator instance
func NewEvaluator(client *mcpclient.Client) *Evaluator {
	return &Evaluator{client: client}
}

// TestOntology is the primary entry point for ontology evaluation
func (e *Evaluator) TestOntology(ctx context.Context, rootPath string, opts EvalOptions) (*EvalResult, error) {
	opts.RootPath = rootPath
	if len(opts.AllowedExtensions) == 0 && len(opts.AllowedExts) > 0 {
		opts.AllowedExtensions = opts.AllowedExts
	}
	return e.Evaluate(ctx, opts)
}

// Evaluate performs real end-to-end evaluation using an ephemeral space and graph_status
func (e *Evaluator) Evaluate(ctx context.Context, opts EvalOptions) (res *EvalResult, err error) {
	if opts.SampleSize <= 0 {
		opts.SampleSize = 5
	}
	if opts.ThresholdOther <= 0 || opts.ThresholdOther > 100 {
		return nil, fmt.Errorf("threshold-other must be between 1 and 100, got %d", opts.ThresholdOther)
	}

	allowedExts := opts.AllowedExtensions
	if len(allowedExts) == 0 && len(opts.AllowedExts) > 0 {
		allowedExts = opts.AllowedExts
	}

	// 1. Discover sample documents in the path
	scanRes, scanErr := scanner.Scan(scanner.ScanOptions{
		RootPath:          opts.RootPath,
		AllowedExtensions: allowedExts,
		BatchSizeMB:       50,
		MaxFileBytes:      opts.MaxFileBytes,
		ForceReplace:      true,
	})
	if scanErr != nil {
		return nil, fmt.Errorf("failed to scan path %s: %w", opts.RootPath, scanErr)
	}

	if scanRes.TotalFiles == 0 {
		return nil, fmt.Errorf("no valid files found in path %s for ontology evaluation", opts.RootPath)
	}

	// Select sample files
	var sampleItems []scanner.FileItem
	var sampleNames []string
	for _, b := range scanRes.Batches {
		for _, f := range b.Files {
			sampleItems = append(sampleItems, f)
			sampleNames = append(sampleNames, f.RelPath)
			if len(sampleItems) >= opts.SampleSize {
				break
			}
		}
		if len(sampleItems) >= opts.SampleSize {
			break
		}
	}

	// 2. Create target space (ephemeral by default)
	targetSpace := opts.SpaceID
	isTemp := false
	if targetSpace == "" {
		isTemp = true
		for attempt := 0; attempt < 5; attempt++ {
			rVal, _ := rand.Int(rand.Reader, big.NewInt(100000))
			candidate := fmt.Sprintf("tmp-eval-%d-%05d", time.Now().Unix(), rVal.Int64())
			createRes, err := e.client.CallTool(ctx, "space_create", map[string]interface{}{
				"space_id":    candidate,
				"description": fmt.Sprintf("Ephemeral ontology evaluation space for %s", opts.RootPath),
				"rules":       "",
			})
			if err == nil {
				createStatus, _ := createRes["status"].(string)
				if createStatus == "created" {
					targetSpace = candidate
					break
				}
			}
		}
		if targetSpace == "" {
			return nil, fmt.Errorf("failed to create unique ephemeral space after multiple attempts")
		}
	} else {
		createRes, err := e.client.CallTool(ctx, "space_create", map[string]interface{}{
			"space_id":    targetSpace,
			"description": fmt.Sprintf("Evaluation space for %s", opts.RootPath),
			"rules":       "",
		})
		if err != nil {
			return nil, fmt.Errorf("failed to create evaluation space %s: %w", targetSpace, err)
		}
		createStatus, _ := createRes["status"].(string)
		if createStatus == "already_exists" && !opts.AllowExistingSpace {
			return nil, fmt.Errorf("evaluation space %s already exists: refusing destructive evaluation against existing space without explicit authorization", targetSpace)
		}
		if createStatus != "created" && createStatus != "already_exists" {
			return nil, fmt.Errorf("failed to initialize space %s with status '%s': %v", targetSpace, createStatus, createRes["message"])
		}
	}

	// Track jobs to cancel before cleanup if an error occurs
	var jobsToPoll []string
	completedJobs := make(map[string]bool)
	submissionAttempted := false
	submissionHarvested := false

	// Install cleanup hook immediately upon space creation
	defer func() {
		if isTemp && !opts.KeepMemory {
			cleanCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()

			allTerminal := submissionHarvested
			if !submissionAttempted {
				allTerminal = true
			}
			// Cancel any active / pending evaluation jobs before deletion and wait for terminal state
			for _, jobID := range jobsToPoll {
				if !completedJobs[jobID] {
					_, cancelErr := e.client.CallTool(cleanCtx, "long_ingest_cancel", map[string]interface{}{
						"space_id": targetSpace,
						"job_id":   jobID,
					})
					jobTerminal := false
					if cancelErr == nil {
						for attempt := 0; attempt < 10; attempt++ {
							stRes, err := e.client.CallTool(cleanCtx, "long_ingest_status", map[string]interface{}{
								"space_id": targetSpace,
								"job_id":   jobID,
							})
							if err == nil && isPositiveTerminalJobState(stRes) {
								completedJobs[jobID] = true
								jobTerminal = true
								break
							}
							time.Sleep(100 * time.Millisecond)
						}
					}
					if !jobTerminal {
						allTerminal = false
					}
				}
			}

			var cleanupStatus string
			var cleanupMsg string

			// Clean up the evaluation space ONLY if submission was never attempted or was fully harvested,
			// AND all discovered jobs have reached a verified positive terminal state
			if (!submissionAttempted || submissionHarvested) && allTerminal {
				delRes, delErr := e.client.CallTool(cleanCtx, "space_delete", map[string]interface{}{
					"space_id": targetSpace,
					"confirm":  true,
				})
				if delErr != nil {
					cleanupStatus = "failed"
					cleanupMsg = fmt.Sprintf("failed to delete temporary space %s: %v", targetSpace, delErr)
				} else if isErr, _ := delRes["isError"].(bool); isErr {
					errMsg, _ := delRes["error"].(string)
					if errMsg == "" {
						errMsg, _ = delRes["message"].(string)
					}
					cleanupStatus = "failed"
					cleanupMsg = fmt.Sprintf("space_delete returned error for %s: %s", targetSpace, errMsg)
				} else {
					st, _ := delRes["status"].(string)
					if strings.EqualFold(strings.TrimSpace(st), "deleted") {
						cleanupStatus = "cleaned"
						cleanupMsg = fmt.Sprintf("Temporary space %s cleaned up", targetSpace)
					} else {
						errMsg, _ := delRes["message"].(string)
						if errMsg == "" {
							errMsg, _ = delRes["error"].(string)
						}
						cleanupStatus = "failed"
						cleanupMsg = fmt.Sprintf("space_delete non-canonical status %q for %s: %s", st, targetSpace, errMsg)
					}
				}
			} else {
				cleanupStatus = "retained"
				cleanupMsg = fmt.Sprintf("temporary space %s retained for investigation (unsettled in-flight jobs or unharvested submission)", targetSpace)
			}

			if res != nil {
				res.SpaceCleanupStatus = cleanupStatus
				res.SpaceCleanupMessage = cleanupMsg
			}
			if err != nil {
				if cleanupStatus == "cleaned" {
					err = fmt.Errorf("%w (ephemeral space %s was cleaned up)", err, targetSpace)
				} else if cleanupStatus == "retained" {
					err = fmt.Errorf("%w (temporary evaluation space %s retained: %s)", err, targetSpace, cleanupMsg)
				} else if cleanupStatus == "failed" {
					err = fmt.Errorf("%w (warning: failed to delete temporary space %s: %s)", err, targetSpace, cleanupMsg)
				}
			}
		} else if isTemp && opts.KeepMemory {
			if res != nil {
				res.SpaceCleanupStatus = "retained"
				res.SpaceCleanupMessage = fmt.Sprintf("Temporary space %s retained as requested (--keep-memory)", targetSpace)
			}
			if err != nil {
				err = fmt.Errorf("%w (temporary evaluation space %s retained as requested via --keep-memory)", err, targetSpace)
			}
		}
	}()

	// 3. Read or resolve ontology YAML (from file path, named schema via ontology_get, or raw YAML string)
	var ontologyYAML string
	if opts.Ontology != "" {
		if fileInfo, err := os.Stat(opts.Ontology); err == nil && fileInfo.Mode().IsRegular() {
			data, err := os.ReadFile(opts.Ontology)
			if err != nil {
				return nil, fmt.Errorf("failed to read ontology YAML file %s: %w", opts.Ontology, err)
			}
			ontologyYAML = string(data)
		} else if strings.Contains(opts.Ontology, "\n") || strings.Contains(opts.Ontology, ":") {
			ontologyYAML = opts.Ontology
		} else {
			// Resolve named ontology via canonical ontology_get API on the initialized target space
			getRes, err := e.client.CallTool(ctx, "ontology_get", map[string]interface{}{
				"space_id": targetSpace,
				"name":     opts.Ontology,
			})
			if err != nil {
				return nil, fmt.Errorf("failed to resolve named ontology %s: %w", opts.Ontology, err)
			}
			if getRes["status"] == "error" || getRes["isError"] == true {
				return nil, fmt.Errorf("failed to fetch ontology %s: %v", opts.Ontology, getRes["message"])
			}
			if cYAML, ok := getRes["content_yaml"].(string); ok && cYAML != "" {
				ontologyYAML = cYAML
			} else if cYAML, ok := getRes["content"].(string); ok && cYAML != "" {
				ontologyYAML = cYAML
			} else if cYAML, ok := getRes["yaml"].(string); ok && cYAML != "" {
				ontologyYAML = cYAML
			} else {
				return nil, fmt.Errorf("ontology %s returned empty schema content", opts.Ontology)
			}
		}
	}

	// 4. Validate ontology YAML with ontology_validate in context of the created space
	if ontologyYAML != "" {
		valRes, err := e.client.CallTool(ctx, "ontology_validate", map[string]interface{}{
			"space_id":     targetSpace,
			"content_yaml": ontologyYAML,
		})
		if err != nil {
			return nil, fmt.Errorf("ontology validation request failed: %w", err)
		}
		if valRes["status"] == "error" || valRes["isError"] == true {
			return nil, fmt.Errorf("invalid ontology YAML: %v", valRes["message"])
		}
		if valid, ok := valRes["valid"].(bool); ok && !valid {
			return nil, fmt.Errorf("ontology schema is invalid: %v", valRes["errors"])
		}
	}

	// 5. Load sample contents and submit ingestion
	var docPayloads []map[string]interface{}
	for _, item := range sampleItems {
		contentObj, err := scanner.LoadFileContent(item.Path, opts.MaxFileBytes)
		if err != nil {
			return nil, fmt.Errorf("failed to load sample %s: %w", item.Path, err)
		}

		doc := map[string]interface{}{
			"source_path":    item.RelPath,
			"filename":       item.Filename,
			"sha256":         contentObj.SHA256,
			"content_base64": contentObj.Base64Data,
			"metadata": map[string]interface{}{
				"content_type": item.ContentType,
			},
		}
		docPayloads = append(docPayloads, doc)
	}

	ingestOpts := map[string]interface{}{
		"replace_existing": true,
	}
	if ontologyYAML != "" {
		ingestOpts["ontology_yaml"] = ontologyYAML
	}

	submissionAttempted = true
	ingestRes, err := e.client.CallTool(ctx, "long_ingest_async", map[string]interface{}{
		"space_id":  targetSpace,
		"documents": docPayloads,
		"options":   ingestOpts,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to submit sample ingestion (temporary evaluation space %q retained for operator investigation): %w", targetSpace, err)
	}

	// Harvest ALL active job IDs across items[], jobs[], and top-level job_id independently
	// before evaluating errors so that deferred cleanup can properly cancel running jobs
	sampleAck := make([]bool, len(sampleItems))
	var firstItemError error
	jobIDSet := make(map[string]bool)

	// 1. Unconditionally harvest all discoverable job IDs so deferred cleanup cancels them
	if topJobID, ok := ingestRes["job_id"].(string); ok && topJobID != "" {
		if !jobIDSet[topJobID] {
			jobIDSet[topJobID] = true
			jobsToPoll = append(jobsToPoll, topJobID)
		}
	}
	if rawItems, exists := ingestRes["items"]; exists && rawItems != nil {
		if l, ok := rawItems.([]interface{}); ok {
			for _, itRaw := range l {
				if itMap, ok := itRaw.(map[string]interface{}); ok {
					if jID, ok := itMap["job_id"].(string); ok && jID != "" {
						if !jobIDSet[jID] {
							jobIDSet[jID] = true
							jobsToPoll = append(jobsToPoll, jID)
						}
					}
				}
			}
		}
	}
	if rawJobs, exists := ingestRes["jobs"]; exists && rawJobs != nil {
		if l, ok := rawJobs.([]interface{}); ok {
			for _, jRaw := range l {
				if jMap, ok := jRaw.(map[string]interface{}); ok {
					if jID, ok := jMap["job_id"].(string); ok && jID != "" {
						if !jobIDSet[jID] {
							jobIDSet[jID] = true
							jobsToPoll = append(jobsToPoll, jID)
						}
					}
				}
			}
		}
	}

	if ingestRes["status"] == "error" || ingestRes["isError"] == true {
		return nil, fmt.Errorf("ingestion error on evaluation (temporary evaluation space %q retained for investigation): %v", targetSpace, ingestRes["message"])
	}

	// 2. Strict response structure validation: reject present-null and non-list types
	var schemaError error
	var itemsList []interface{}
	if rawItems, exists := ingestRes["items"]; exists {
		if rawItems == nil {
			schemaError = fmt.Errorf("sample ingestion response 'items' field cannot be null")
		} else if l, ok := rawItems.([]interface{}); ok {
			itemsList = l
		} else {
			schemaError = fmt.Errorf("sample ingestion response 'items' field must be a list")
		}
	}

	var jobsList []interface{}
	if rawJobs, exists := ingestRes["jobs"]; exists {
		if rawJobs == nil {
			if schemaError == nil {
				schemaError = fmt.Errorf("sample ingestion response 'jobs' field cannot be null")
			}
		} else if l, ok := rawJobs.([]interface{}); ok {
			jobsList = l
		} else {
			if schemaError == nil {
				schemaError = fmt.Errorf("sample ingestion response 'jobs' field must be a list")
			}
		}
	}

	if schemaError != nil {
		return nil, schemaError
	}

	matchSample := func(itMap map[string]interface{}) (int, error) {
		var sourcePath, filename string
		if rawSP, exists := itMap["source_path"]; exists {
			if rawSP == nil {
				return -1, fmt.Errorf("source_path field cannot be null")
			}
			spStr, ok := rawSP.(string)
			if !ok {
				return -1, fmt.Errorf("source_path field must be a string")
			}
			if strings.TrimSpace(spStr) == "" {
				return -1, fmt.Errorf("source_path field cannot be blank")
			}
			sourcePath = spStr
		}
		if rawFN, exists := itMap["filename"]; exists {
			if rawFN == nil {
				return -1, fmt.Errorf("filename field cannot be null")
			}
			fnStr, ok := rawFN.(string)
			if !ok {
				return -1, fmt.Errorf("filename field must be a string")
			}
			if strings.TrimSpace(fnStr) == "" {
				return -1, fmt.Errorf("filename field cannot be blank")
			}
			filename = fnStr
		}

		if rawIdx, hasIndex := itMap["index"]; hasIndex {
			if rawIdx == nil {
				return -1, fmt.Errorf("sample index cannot be null")
			}
			idx, err := parseStrictNonNegativeInt(rawIdx, "item.index")
			if err != nil {
				return -1, fmt.Errorf("malformed sample index: %w", err)
			}
			if idx >= len(sampleItems) {
				return -1, fmt.Errorf("sample index %d out of bounds (total samples: %d)", idx, len(sampleItems))
			}
			targetSample := sampleItems[idx]
			if sourcePath != "" && targetSample.RelPath != sourcePath {
				return -1, fmt.Errorf("conflicting acknowledgement: index %d has rel_path '%s' which does not match source_path '%s'", idx, targetSample.RelPath, sourcePath)
			}
			if filename != "" && targetSample.Filename != filename {
				return -1, fmt.Errorf("conflicting acknowledgement: index %d has filename '%s' which does not match filename '%s'", idx, targetSample.Filename, filename)
			}
			return idx, nil
		}

		// If no index is provided and source_path is present, match by exact RelPath only
		if sourcePath != "" {
			for i, s := range sampleItems {
				if s.RelPath == sourcePath {
					if filename != "" && s.Filename != filename {
						return -1, fmt.Errorf("conflicting acknowledgement: source_path '%s' has filename '%s' but response gave '%s'", sourcePath, s.Filename, filename)
					}
					return i, nil
				}
			}
			return -1, fmt.Errorf("unrecognized sample source_path '%s'", sourcePath)
		}

		// If only filename is provided, match by Filename ONLY IF unique among all sampleItems
		if filename != "" {
			matchIdx := -1
			matchCount := 0
			for i, s := range sampleItems {
				if s.Filename == filename {
					matchIdx = i
					matchCount++
				}
			}
			if matchCount == 1 {
				return matchIdx, nil
			}
			if matchCount > 1 {
				return -1, fmt.Errorf("ambiguous acknowledgement: multiple samples share filename '%s' without explicit index or distinct relative path", filename)
			}
			return -1, fmt.Errorf("unrecognized sample filename '%s'", filename)
		}

		return -1, fmt.Errorf("unrecognized sample in ingestion response: missing index, source_path, and filename")
	}

	ackSample := func(matchedIdx int) error {
		if matchedIdx < 0 || matchedIdx >= len(sampleAck) {
			return nil
		}
		if sampleAck[matchedIdx] {
			return fmt.Errorf("sample item %s was acknowledged multiple times", sampleItems[matchedIdx].RelPath)
		}
		sampleAck[matchedIdx] = true
		return nil
	}

	if len(itemsList) > 0 {
		for _, itRaw := range itemsList {
			itMap, ok := itRaw.(map[string]interface{})
			if !ok {
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion item must be an object")
				}
				continue
			}
			matchedIdx, matchErr := matchSample(itMap)
			if matchErr != nil && firstItemError == nil {
				firstItemError = matchErr
			}
			sourcePath, _ := itMap["source_path"].(string)
			if sourcePath == "" {
				sourcePath, _ = itMap["filename"].(string)
			}
			jobID, _ := itMap["job_id"].(string)
			itemStatus, _ := itMap["status"].(string)
			kind := classifyJobStatus(itemStatus)
			switch kind {
			case StatusSuccess:
				if matchedIdx >= 0 {
					if ackErr := ackSample(matchedIdx); ackErr != nil && firstItemError == nil {
						firstItemError = ackErr
					}
				}
			case StatusSkipped:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion item %s was skipped (%s) without active extraction", sourcePath, itemStatus)
				}
			case StatusPending:
				if jobID == "" {
					if firstItemError == nil {
						firstItemError = fmt.Errorf("sample ingestion item %s returned pending status '%s' without job_id", sourcePath, itemStatus)
					}
				} else {
					if !jobIDSet[jobID] {
						jobIDSet[jobID] = true
						jobsToPoll = append(jobsToPoll, jobID)
					}
					if matchedIdx >= 0 {
						if ackErr := ackSample(matchedIdx); ackErr != nil && firstItemError == nil {
							firstItemError = ackErr
						}
					}
				}
			case StatusFailed:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion item %s failed: %v", sourcePath, itMap["message"])
				}
			default:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion item %s returned unknown status '%s'", sourcePath, itemStatus)
				}
			}
		}
	}

	if len(jobsList) > 0 {
		for _, jRaw := range jobsList {
			jMap, ok := jRaw.(map[string]interface{})
			if !ok {
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion job must be an object")
				}
				continue
			}
			matchedIdx, matchErr := matchSample(jMap)
			if matchErr != nil && firstItemError == nil {
				firstItemError = matchErr
			}
			sourcePath, _ := jMap["source_path"].(string)
			if sourcePath == "" {
				sourcePath, _ = jMap["filename"].(string)
			}
			jobID, _ := jMap["job_id"].(string)
			jobStatus, _ := jMap["status"].(string)
			kind := classifyJobStatus(jobStatus)
			switch kind {
			case StatusSuccess:
				if matchedIdx >= 0 {
					if ackErr := ackSample(matchedIdx); ackErr != nil && firstItemError == nil {
						firstItemError = ackErr
					}
				}
			case StatusSkipped:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion job %s was skipped (%s) without active extraction", sourcePath, jobStatus)
				}
			case StatusPending:
				if jobID == "" {
					if firstItemError == nil {
						firstItemError = fmt.Errorf("sample ingestion job %s returned pending status '%s' without job_id", sourcePath, jobStatus)
					}
				} else {
					if !jobIDSet[jobID] {
						jobIDSet[jobID] = true
						jobsToPoll = append(jobsToPoll, jobID)
					}
					if matchedIdx >= 0 {
						if ackErr := ackSample(matchedIdx); ackErr != nil && firstItemError == nil {
							firstItemError = ackErr
						}
					}
				}
			case StatusFailed:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion job %s failed: %v", sourcePath, jMap["message"])
				}
			default:
				if firstItemError == nil {
					firstItemError = fmt.Errorf("sample ingestion job %s returned unknown status '%s'", sourcePath, jobStatus)
				}
			}
		}
	}

	// Check explicit errors list fail-closed
	if errorsList, ok := ingestRes["errors"].([]interface{}); ok && len(errorsList) > 0 {
		return nil, fmt.Errorf("batch ingestion errors occurred during evaluation: %+v", errorsList)
	}

	// Unconditional envelope status classification
	topStatus, _ := ingestRes["status"].(string)
	topJobID, _ := ingestRes["job_id"].(string)
	topKind := classifyEnvelopeStatus(topStatus)
	switch topKind {
	case StatusSuccess:
		// Envelope marked success
	case StatusSkipped:
		return nil, fmt.Errorf("sample ingestion envelope was skipped (%s) without active extraction", topStatus)
	case StatusPending:
		if topJobID != "" {
			if !jobIDSet[topJobID] {
				jobIDSet[topJobID] = true
				jobsToPoll = append(jobsToPoll, topJobID)
			}
		} else if len(jobsToPoll) == 0 {
			return nil, fmt.Errorf("sample ingestion envelope returned pending status '%s' without job_id", topStatus)
		}
	case StatusFailed:
		errMsg, _ := ingestRes["message"].(string)
		if errMsg == "" {
			errMsg = fmt.Sprintf("envelope ended with status '%s'", topStatus)
		}
		return nil, fmt.Errorf("sample ingestion envelope failed: %s", errMsg)
	default:
		return nil, fmt.Errorf("sample ingestion returned unrecognized envelope status '%s'", topStatus)
	}

	if firstItemError != nil {
		return nil, fmt.Errorf("sample ingestion item error (temporary evaluation space %q retained): %w", targetSpace, firstItemError)
	}

	if len(itemsList) == 0 && len(jobsList) == 0 && topJobID == "" {
		return nil, fmt.Errorf("sample ingestion response missing items or jobs detail list (temporary evaluation space %q retained)", targetSpace)
	}

	// Verify all submitted samples are strictly acknowledged
	for i, s := range sampleItems {
		if !sampleAck[i] {
			return nil, fmt.Errorf("sample item %s was not acknowledged in ingestion response (temporary evaluation space %q retained)", s.RelPath, targetSpace)
		}
	}
	submissionHarvested = true

	pollTimeout := opts.Timeout
	if pollTimeout <= 0 {
		pollTimeout = 60 * time.Second
	}

	// 5. Wait for all pending jobs to finish
	for _, jobID := range jobsToPoll {
		if err := e.pollJob(ctx, targetSpace, jobID, pollTimeout); err != nil {
			return nil, err
		}
		completedJobs[jobID] = true
	}

	// 6. Query long_status to inspect graph statistics
	statusRes, err := e.client.CallTool(ctx, "long_status", map[string]interface{}{
		"space_id":      targetSpace,
		"include_graph": true,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to retrieve graph statistics for ontology evaluation: %w", err)
	}

	graphStats, ok := statusRes["graph_stats"].(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("long_status response missing graph_stats")
	}

	res = &EvalResult{
		OntologyPath:         opts.Ontology,
		SpaceID:              targetSpace,
		IsTemporarySpace:     isTemp,
		SampleCount:          len(sampleItems),
		SampleFiles:          sampleNames,
		ThresholdOther:       opts.ThresholdOther,
		TotalEntities:        0,
		TypedEntities:        0,
		OtherEntities:        0,
		OtherPercentage:      0.0,
		EntityTypesBreakdown: make(map[string]int),
		UnclassifiedConcepts: make([]UnclassifiedConcept, 0),
		Suggestions:          make([]string, 0),
	}

	// Extract total entity count strictly (supports entity_count and entities_count)
	if rawTotal, ok := graphStats["entity_count"]; ok && rawTotal != nil {
		parsedTotal, err := parseStrictNonNegativeInt(rawTotal, "graph_stats.entity_count")
		if err != nil {
			return nil, err
		}
		res.TotalEntities = parsedTotal
	} else if rawTotal, ok := graphStats["entities_count"]; ok && rawTotal != nil {
		parsedTotal, err := parseStrictNonNegativeInt(rawTotal, "graph_stats.entities_count")
		if err != nil {
			return nil, err
		}
		res.TotalEntities = parsedTotal
	}

	// Extract entity type breakdown strictly
	counted := 0
	hasFullTypeDist := false
	if rawTypes, ok := graphStats["entity_types"].(map[string]interface{}); ok && len(rawTypes) > 0 {
		hasFullTypeDist = true
		for typeName, rawCount := range rawTypes {
			count, err := parseStrictNonNegativeInt(rawCount, fmt.Sprintf("entity_types[%s]", typeName))
			if err != nil {
				return nil, err
			}
			res.EntityTypesBreakdown[typeName] = count
			counted += count
			if strings.EqualFold(typeName, "Other") || strings.EqualFold(typeName, "Generic") {
				res.OtherEntities += count
			} else {
				res.TypedEntities += count
			}
		}
	}

	// If entity_types map was not directly in graph_stats, build breakdown from graph_view and top_entities
	if !hasFullTypeDist {
		countedEntityIDs := make(map[string]bool)

		// 1. graph_view.nodes
		if graphView, ok := statusRes["graph_view"].(map[string]interface{}); ok {
			if nodes, ok := graphView["nodes"].([]interface{}); ok {
				for _, nRaw := range nodes {
					if nMap, ok := nRaw.(map[string]interface{}); ok {
						nodeType, _ := nMap["node_type"].(string)
						if strings.EqualFold(nodeType, "document") {
							continue
						}
						eType, _ := nMap["type"].(string)
						if strings.EqualFold(eType, "document") {
							continue
						}
						if eType == "" {
							eType = "Other"
						}
						nodeID, _ := nMap["id"].(string)
						if nodeID == "" {
							nodeID = fmt.Sprintf("node-%s-%s", nMap["name"], eType)
						}
						if !countedEntityIDs[nodeID] {
							countedEntityIDs[nodeID] = true
							res.EntityTypesBreakdown[eType]++
							counted++
							if strings.EqualFold(eType, "Other") || strings.EqualFold(eType, "Generic") {
								res.OtherEntities++
							} else {
								res.TypedEntities++
							}
						}
					}
				}
			}
		}

		// 2. top_entities
		if len(res.EntityTypesBreakdown) == 0 {
			if topEnts, ok := statusRes["top_entities"].([]interface{}); ok {
				for idx, entRaw := range topEnts {
					if entMap, ok := entRaw.(map[string]interface{}); ok {
						eType, _ := entMap["type"].(string)
						if strings.EqualFold(eType, "document") {
							continue
						}
						if eType == "" {
							eType = "Other"
						}
						eName, _ := entMap["name"].(string)
						if eName == "" {
							eName, _ = entMap["entity"].(string)
						}
						entKey := fmt.Sprintf("top-%d-%s-%s", idx, eName, eType)
						if !countedEntityIDs[entKey] {
							countedEntityIDs[entKey] = true
							res.EntityTypesBreakdown[eType]++
							counted++
							if strings.EqualFold(eType, "Other") || strings.EqualFold(eType, "Generic") {
								res.OtherEntities++
							} else {
								res.TypedEntities++
							}
						}
					}
				}
			}
		}
	}

	if res.TotalEntities != counted {
		return nil, fmt.Errorf("cannot establish complete entity-type distribution: total entities in graph (%d) does not match available entity details (%d)", res.TotalEntities, counted)
	}

	// Extract top unclassified entities
	if topEnts, ok := statusRes["top_entities"].([]interface{}); ok {
		for _, entRaw := range topEnts {
			if entMap, ok := entRaw.(map[string]interface{}); ok {
				eType, _ := entMap["type"].(string)
				if strings.EqualFold(eType, "Other") || strings.EqualFold(eType, "Generic") || eType == "" {
					cName, _ := entMap["name"].(string)
					if cName == "" {
						cName, _ = entMap["entity"].(string)
					}
					cCount := 1
					if countVal, ok := entMap["mentions"]; ok && countVal != nil {
						parsed, err := parseStrictNonNegativeInt(countVal, fmt.Sprintf("unclassified concept '%s' mentions", cName))
						if err != nil {
							return nil, err
						}
						cCount = parsed
					} else if countVal, ok := entMap["count"]; ok && countVal != nil {
						parsed, err := parseStrictNonNegativeInt(countVal, fmt.Sprintf("unclassified concept '%s' count", cName))
						if err != nil {
							return nil, err
						}
						cCount = parsed
					}
					if cName != "" {
						res.UnclassifiedConcepts = append(res.UnclassifiedConcepts, UnclassifiedConcept{
							Name:      cName,
							Frequency: cCount,
						})
					}
				}
			}
		}
	}

	if res.TotalEntities == 0 {
		return nil, fmt.Errorf("no entities were extracted from the sample documents during ontology evaluation")
	}

	// 7. Calculate adequacy metrics
	res.OtherPercentage = (float64(res.OtherEntities) / float64(res.TotalEntities)) * 100.0

	// Determine relevance tier
	if res.OtherPercentage <= 10.0 {
		res.RelevanceTier = "High"
	} else if res.OtherPercentage <= 25.0 {
		res.RelevanceTier = "Moderate"
	} else {
		res.RelevanceTier = "Low"
	}

	if res.OtherPercentage > float64(opts.ThresholdOther) {
		res.PassedThreshold = false
		res.ExtractionStatus = "warning"
		res.Message = fmt.Sprintf("Ontology adequacy alert: %.1f%% of extracted entities are unclassified 'Other' (threshold: %d%%).", res.OtherPercentage, opts.ThresholdOther)
		res.Suggestions = append(res.Suggestions, "Enrich ontology YAML with dedicated entity types for the unclassified concepts listed in the report.")
	} else {
		res.PassedThreshold = true
		res.ExtractionStatus = "success"
		res.Message = fmt.Sprintf("Ontology adequacy verified: %.1f%% unclassified entities (Tier: %s, threshold: %d%%).", res.OtherPercentage, res.RelevanceTier, opts.ThresholdOther)
	}

	return res, nil
}

// StatusKind defines the classification category of an ingestion status string
type StatusKind int

const (
	StatusUnknown StatusKind = iota
	StatusSuccess
	StatusPending
	StatusFailed
	StatusSkipped
)

func isPositiveTerminalJobState(res map[string]interface{}) bool {
	if res == nil {
		return false
	}
	if isErr, ok := res["isError"].(bool); ok && isErr {
		return false
	}
	topStatus, _ := res["status"].(string)
	if strings.EqualFold(strings.TrimSpace(topStatus), "error") {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(topStatus)) {
	case "succeeded", "failed", "cancelled", "skipped", "changed_skipped":
		return true
	default:
		return false
	}
}

func isTerminalStatus(status string) bool {
	kind := classifyJobStatus(status)
	return kind == StatusSuccess || kind == StatusSkipped || kind == StatusFailed
}

func classifyJobStatus(status string) StatusKind {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "succeeded":
		return StatusSuccess
	case "skipped", "changed_skipped":
		return StatusSkipped
	case "queued", "running", "processing", "pending", "in_progress":
		return StatusPending
	case "failed", "cancelled":
		return StatusFailed
	default:
		return StatusUnknown
	}
}

func classifyEnvelopeStatus(status string) StatusKind {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "completed", "success", "succeeded", "ok":
		return StatusSuccess
	case "skipped", "changed_skipped":
		return StatusSkipped
	case "queued", "running", "processing", "pending", "in_progress":
		return StatusPending
	case "error", "failed", "queue_full", "cancelled", "rejected":
		return StatusFailed
	default:
		return StatusUnknown
	}
}

func (e *Evaluator) pollJob(ctx context.Context, spaceID, jobID string, pollTimeout time.Duration) error {
	if pollTimeout <= 0 {
		pollTimeout = 60 * time.Second
	}
	interval := 500 * time.Millisecond
	if e.PollInterval > 0 {
		interval = e.PollInterval
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	timeout := time.After(pollTimeout)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timeout:
			return fmt.Errorf("timeout waiting for evaluation job %s", jobID)
		case <-ticker.C:
			res, err := e.client.CallTool(ctx, "long_ingest_status", map[string]interface{}{
				"space_id": spaceID,
				"job_id":   jobID,
			})
			if err != nil {
				return err
			}
			if isErr, _ := res["isError"].(bool); isErr {
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = "status lookup returned MCP error"
				}
				return fmt.Errorf("evaluation ingestion job %s status query failed: %s", jobID, errMsg)
			}
			topStatus, _ := res["status"].(string)
			if strings.EqualFold(strings.TrimSpace(topStatus), "error") {
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = "status lookup returned status error"
				}
				return fmt.Errorf("evaluation ingestion job %s status query failed: %s", jobID, errMsg)
			}
			status := topStatus
			kind := classifyJobStatus(status)
			switch kind {
			case StatusSuccess:
				return nil
			case StatusSkipped:
				return fmt.Errorf("evaluation ingestion job %s ended with skipped status without active extraction", jobID)
			case StatusFailed:
				errMsg, _ := res["error"].(string)
				if errMsg == "" {
					errMsg, _ = res["message"].(string)
				}
				if errMsg == "" {
					errMsg = fmt.Sprintf("job ended with status %s", status)
				}
				return fmt.Errorf("evaluation ingestion job failed: %s", errMsg)
			case StatusPending:
				// continue polling
			default:
				return fmt.Errorf("pollJob received non-job/unrecognized status %q", status)
			}
		}
	}
}

const MaxSafeEntityCount = 1<<53 - 1 // 9007199254740991 (IEEE-754 exact integer limit)

// parseStrictNonNegativeInt strictly validates that a JSON value is a non-negative integral integer within safe bounds
func parseStrictNonNegativeInt(val interface{}, name string) (int, error) {
	switch v := val.(type) {
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) || v < 0 || v != math.Floor(v) || v > float64(MaxSafeEntityCount) {
			return 0, fmt.Errorf("invalid entity count for %s: %v (must be a non-negative integer <= %d)", name, v, MaxSafeEntityCount)
		}
		return int(v), nil
	case int:
		if v < 0 {
			return 0, fmt.Errorf("invalid negative entity count for %s: %d", name, v)
		}
		return v, nil
	case int64:
		if v < 0 || v > int64(MaxSafeEntityCount) {
			return 0, fmt.Errorf("invalid entity count for %s: %d", name, v)
		}
		return int(v), nil
	default:
		return 0, fmt.Errorf("unsupported non-numeric entity count type %T for %s", val, name)
	}
}
