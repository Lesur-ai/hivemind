package ingest

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/scanner"
)

func newMockServer(toolHandler func(name string, args map[string]interface{}) (string, bool)) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req mcpclient.JSONRPCRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")

		if req.Method == "initialize" {
			w.Header().Set("mcp-session-id", "mock-session-id")
			w.Header().Set("mcp-protocol-version", "2024-11-05")
			resp := mcpclient.JSONRPCResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result:  json.RawMessage(`{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"test","version":"1.0"}}`),
			}
			_ = json.NewEncoder(w).Encode(resp)
			return
		}

		if req.Method == "notifications/initialized" {
			w.WriteHeader(http.StatusOK)
			return
		}

		if req.Method == "tools/call" {
			params, _ := req.Params.(map[string]interface{})
			name, _ := params["name"].(string)
			args, _ := params["arguments"].(map[string]interface{})
			resBody, isErr := toolHandler(name, args)
			resp := mcpclient.JSONRPCResponse{
				JSONRPC: "2.0",
				ID:      req.ID,
				Result:  json.RawMessage(`{"content":[{"type":"text","text":` + string(mustJSON(resBody)) + `}],"isError":` + fmt.Sprintf("%t", isErr) + `}`),
			}
			_ = json.NewEncoder(w).Encode(resp)
			return
		}

		resp := mcpclient.JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result:  json.RawMessage(`{"status":"ok"}`),
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
}

func TestEngineRunSuccess(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "sample.md")
	_ = os.WriteFile(docPath, []byte("# Test Document\nSome content"), 0644)

	jobPollCount := 0
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			return `{"status":"ok","documents":[]}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-123","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"sample.md","job_id":"job-12345","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status", "long_ingest_job_status":
			jobPollCount++
			if jobPollCount >= 2 {
				return `{"status":"succeeded","job_id":"job-12345"}`, false
			}
			return `{"status":"running","job_id":"job-12345"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:              tempDir,
		SpaceID:           "demo-space",
		AllowedExtensions: []string{".md"},
		WatchJobs:         true,
		Timeout:           5 * time.Second,
	}

	res, err := engine.Run(context.Background(), opts, nil)
	if err != nil {
		t.Fatalf("engine.Run failed: %v", err)
	}

	if !res.Success {
		t.Errorf("expected success true, got false")
	}
	if res.TotalUploaded != 1 {
		t.Errorf("expected 1 uploaded file, got %d", res.TotalUploaded)
	}
	if res.TotalSucceeded != 1 {
		t.Errorf("expected 1 succeeded file, got %d", res.TotalSucceeded)
	}
}

func TestEngineAsyncJobFailure(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "corrupted.md")
	_ = os.WriteFile(docPath, []byte("# Corrupted Document"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			return `{"status":"ok","documents":[]}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-123","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"corrupted.md","job_id":"job-fail-99","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status", "long_ingest_job_status":
			return `{"status":"failed","job_id":"job-fail-99","error":"graph extraction memory limit exceeded"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:              tempDir,
		SpaceID:           "demo-space",
		AllowedExtensions: []string{".md"},
		WatchJobs:         true,
		Timeout:           5 * time.Second,
	}

	res, err := engine.Run(context.Background(), opts, nil)
	if err != nil {
		t.Fatalf("engine.Run should not error at top-level on partial job failure, got: %v", err)
	}

	if res.Success {
		t.Errorf("expected success false on failed job, got true")
	}
	if res.TotalFailed != 1 {
		t.Errorf("expected 1 failed job, got %d", res.TotalFailed)
	}
	if len(res.Jobs) != 1 || res.Jobs[0].Error != "graph extraction memory limit exceeded" {
		t.Errorf("expected job error captured, got: %+v", res.Jobs)
	}
}

func TestEngineDeduplicationAndForceReplace(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "existing.md")
	content := []byte("# Existing Document")
	_ = os.WriteFile(docPath, content, 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			// Valid 64-char SHA-256 of "# Existing Document" with explicit source_path and completed status
			return `{"status":"ok","documents":[{"source_path":"existing.md","sha256":"88e62a89c10acc7f3ec78ff2df1b2a47386b7a36310a1372ee50a95125857486","ingestion_status":"completed"}]}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-123","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"existing.md","job_id":"job-replace","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded","job_id":"job-replace"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	// 1. Without ForceReplace: should be skipped
	opts := IngestOptions{
		Path:              tempDir,
		SpaceID:           "demo-space",
		AllowedExtensions: []string{".md"},
		ForceReplace:      false,
	}
	res, err := engine.Run(context.Background(), opts, nil)
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}
	if res.TotalSkipped != 1 {
		t.Errorf("expected 1 skipped file, got %d", res.TotalSkipped)
	}
	if res.TotalUploaded != 0 {
		t.Errorf("expected 0 uploaded files, got %d", res.TotalUploaded)
	}

	// 2. With ForceReplace: should upload even if hash is known
	opts.ForceReplace = true
	res2, err := engine.Run(context.Background(), opts, nil)
	if err != nil {
		t.Fatalf("Run with ForceReplace failed: %v", err)
	}
	if res2.TotalUploaded != 1 {
		t.Errorf("expected 1 uploaded file with ForceReplace, got %d", res2.TotalUploaded)
	}
}

func TestEngineMissingSpaceFailClosed(t *testing.T) {
	tempDir := t.TempDir()
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		return `{"status":"not_found","message":"space not found"}`, false
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:                 tempDir,
		SpaceID:              "non-existent-space",
		CreateSpaceIfMissing: false,
	}

	_, err := engine.Run(context.Background(), opts, nil)
	if err == nil {
		t.Fatal("expected error on missing space when CreateSpaceIfMissing is false, got nil")
	}
}

func TestEngineAuthErrorDoesNotCreateSpace(t *testing.T) {
	tempDir := t.TempDir()
	spaceCreateCalled := false

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		if name == "space_create" {
			spaceCreateCalled = true
		}
		return `{"status":"error","message":"Access denied to space 'secret-space'"}`, false
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:                 tempDir,
		SpaceID:              "secret-space",
		CreateSpaceIfMissing: true, // Even if requested, must fail closed on auth error without calling space_create
	}

	_, err := engine.Run(context.Background(), opts, nil)
	if err == nil {
		t.Fatal("expected error on auth denied space, got nil")
	}
	if spaceCreateCalled {
		t.Fatal("space_create was called on auth error; expected fail-closed behavior")
	}
}

func TestEngineSpaceCreatePartialFails(t *testing.T) {
	tempDir := t.TempDir()
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"not_found","message":"space does not exist"}`, false
		case "space_create":
			// Return partial status (recovery required)
			return `{"status":"partial","message":"Space creation incomplete, recovery required"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:                 tempDir,
		SpaceID:              "partial-space",
		CreateSpaceIfMissing: true,
	}

	_, err := engine.Run(context.Background(), opts, nil)
	if err == nil {
		t.Fatal("expected Run to fail when space_create returns status: partial, got nil")
	}
}

func TestEngineMalformedCatalogEntryFailClosed(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Doc"), 0644)

	ingestCalled := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			// Catalog entry with invalid (non-64 hex) sha256 checksum on eligible document
			return `{"status":"ok","documents":[{"source_path":"bad.md","ingestion_status":"completed","sha256":"invalid_short_hash"}]}`, false
		case "long_ingest_async":
			ingestCalled = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:    tempDir,
		SpaceID: "demo-space",
	}

	_, err := engine.Run(context.Background(), opts, nil)
	if err == nil {
		t.Fatal("expected Run to fail closed on malformed catalog sha256, got nil")
	}
	if ingestCalled {
		t.Fatal("long_ingest_async was called despite malformed catalog SHA256; expected fail-closed")
	}
}

func TestEngineBatchIngestionContractItemsAndErrors(t *testing.T) {
	tempDir := t.TempDir()
	doc1 := filepath.Join(tempDir, "ok.md")
	doc2 := filepath.Join(tempDir, "fail.md")
	_ = os.WriteFile(doc1, []byte("# OK file"), 0644)
	_ = os.WriteFile(doc2, []byte("# Fail file"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		if name == "long_ingest_async" {
			// Verify payload contains content_base64
			docs, _ := args["documents"].([]interface{})
			if len(docs) != 2 {
				return fmt.Sprintf(`{"status":"error","message":"expected 2 docs, got %d"}`, len(docs)), false
			}
			for _, dRaw := range docs {
				d, _ := dRaw.(map[string]interface{})
				if d["content_base64"] == nil || d["content_base64"] == "" {
					return `{"status":"error","message":"missing content_base64 in document payload"}`, false
				}
			}

			// Return canonical memory_ingest_batch_async response with mixed items & errors
			return `{
				"status": "ok",
				"batch_id": "batch-mix",
				"total": 2,
				"counts": {"queued": 1, "failed": 1},
				"items": [
					{"index": 0, "source_path": "ok.md", "job_id": "job-ok-1", "status": "queued"},
					{"index": 1, "source_path": "fail.md", "status": "error", "message": "unsupported binary format"}
				],
				"errors": [
					{"source_path": "fail.md", "filename": "fail.md", "error": "unsupported binary format"}
				]
			}`, false
		}
		return `{"status":"ok"}`, false
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	batch := scanner.Batch{
		Files: []scanner.FileItem{
			{Path: doc1, RelPath: "ok.md", Filename: "ok.md", SHA256: "abc1"},
			{Path: doc2, RelPath: "fail.md", Filename: "fail.md", SHA256: "abc2"},
		},
	}

	records, err := engine.IngestBatch(context.Background(), "demo-space", batch, "", 10*1024*1024, false)
	if err != nil {
		t.Fatalf("IngestBatch failed: %v", err)
	}

	if len(records) != 2 {
		t.Fatalf("expected 2 job records, got %d", len(records))
	}

	var okRec, failRec *JobRecord
	for _, r := range records {
		if r.Filename == "ok.md" {
			okRec = r
		} else if r.Filename == "fail.md" {
			failRec = r
		}
	}

	if okRec == nil || okRec.JobID != "job-ok-1" || okRec.Status != "queued" {
		t.Errorf("okRec mismatch: %+v", okRec)
	}
	if failRec == nil || failRec.Status != "error" || failRec.Error != "unsupported binary format" {
		t.Errorf("failRec mismatch: %+v", failRec)
	}
}

func TestEngineFailedCatalogEntryRetries(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "retry.md")
	content := []byte("# Document To Retry")
	_ = os.WriteFile(docPath, content, 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			// Catalog has entry with failed ingestion_status
			return `{"status":"ok","documents":[{"source_path":"retry.md","sha256":"65463f253818e69247d48383c276ceb72b834ef19c36203cfc4cfef1b702ec80","ingestion_status":"failed"}]}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-123","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"retry.md","job_id":"job-retry-ok","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded","job_id":"job-retry-ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	opts := IngestOptions{
		Path:              tempDir,
		SpaceID:           "demo-space",
		AllowedExtensions: []string{".md"},
		ForceReplace:      false,
	}
	res, err := engine.Run(context.Background(), opts, nil)
	if err != nil {
		t.Fatalf("Run failed: %v", err)
	}

	// Should NOT be skipped because catalog status was failed
	if res.TotalUploaded != 1 {
		t.Errorf("expected 1 uploaded file for failed entry retry, got %d", res.TotalUploaded)
	}
	if res.TotalSkipped != 0 {
		t.Errorf("expected 0 skipped files, got %d", res.TotalSkipped)
	}
}

func TestEnginePathlessAndLegacyCatalogEntriesIgnored(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Content"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"demo-space"}`, false
		case "long_ingest_list", "long_document_list":
			// Contains pathless entry with null/malformed sha256, plus valid entry
			return `{"status":"ok","documents":[{"filename":"legacy.md","sha256":null},{"source_path":"","sha256":"invalid"},{"source_path":"doc.md","sha256":"65463f253818e69247d48383c276ceb72b834ef19c36203cfc4cfef1b702ec80","ingestion_status":"completed"}]}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-123","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"doc.md","job_id":"job-ok","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded","job_id":"job-ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	known, err := engine.FetchKnownDocuments(context.Background(), "demo-space")
	if err != nil {
		t.Fatalf("FetchKnownDocuments should not fail on pathless entries with malformed hashes, got: %v", err)
	}

	if len(known) != 1 || known["doc.md"] != "65463f253818e69247d48383c276ceb72b834ef19c36203cfc4cfef1b702ec80" {
		t.Errorf("expected only valid doc.md in known map, got: %+v", known)
	}
}

func TestEngineEnsureSpaceRuleValidation(t *testing.T) {
	tempDir := t.TempDir()
	rulesFile := filepath.Join(tempDir, "custom_rules.md")
	_ = os.WriteFile(rulesFile, []byte("# Custom Rules\nRule 1: Be helpful"), 0644)

	var lastRulesSent string
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"not_found"}`, false
		case "space_create":
			lastRulesSent, _ = args["rules"].(string)
			return `{"status":"created"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	// 1. Standard rules template -> empty string sent to server
	if err := engine.EnsureSpace(context.Background(), "sp1", true, "standard"); err != nil {
		t.Fatalf("EnsureSpace standard failed: %v", err)
	}
	if lastRulesSent != "" {
		t.Errorf("expected empty rules string for standard, got %q", lastRulesSent)
	}

	// 2. Rules from file path -> file content sent to server
	if err := engine.EnsureSpace(context.Background(), "sp2", true, rulesFile); err != nil {
		t.Fatalf("EnsureSpace rules file failed: %v", err)
	}
	if lastRulesSent != "# Custom Rules\nRule 1: Be helpful" {
		t.Errorf("expected file content rules, got %q", lastRulesSent)
	}

	// 3. Raw Markdown text rules
	if err := engine.EnsureSpace(context.Background(), "sp3", true, "# Raw Markdown\nSome rules"); err != nil {
		t.Fatalf("EnsureSpace raw markdown failed: %v", err)
	}
	if lastRulesSent != "# Raw Markdown\nSome rules" {
		t.Errorf("expected raw markdown rules, got %q", lastRulesSent)
	}

	// 4. Non-Markdown file -> rejected fail-closed
	nonMdRulesFile := filepath.Join(tempDir, "custom_rules.txt")
	_ = os.WriteFile(nonMdRulesFile, []byte("Some rules"), 0644)
	if err := engine.EnsureSpace(context.Background(), "sp4", true, nonMdRulesFile); err == nil {
		t.Fatalf("expected error for non-md rules file, got nil")
	}

	// 5. Invalid template name -> rejected fail-closed
	if err := engine.EnsureSpace(context.Background(), "sp5", true, "unknown-template-xyz"); err == nil {
		t.Fatalf("expected error for unknown rules template, got nil")
	}
}

func TestEngineResolveOntologyFileAndNamed(t *testing.T) {
	tempDir := t.TempDir()
	yamlFile := filepath.Join(tempDir, "schema.yaml")
	_ = os.WriteFile(yamlFile, []byte("entities:\n  - name: Server"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_get":
			if args["name"] == "software" {
				return `{"status":"ok","content_yaml":"entities:\n  - name: App"}`, false
			}
			return `{"status":"error","message":"unknown ontology"}`, false
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	// 1. Resolve from local file path
	yaml1, err := engine.ResolveOntology(context.Background(), "sp1", yamlFile)
	if err != nil {
		t.Fatalf("ResolveOntology from file failed: %v", err)
	}
	if yaml1 != "entities:\n  - name: Server" {
		t.Errorf("unexpected yaml content from file: %q", yaml1)
	}

	// 2. Resolve from named ontology
	yaml2, err := engine.ResolveOntology(context.Background(), "sp1", "software")
	if err != nil {
		t.Fatalf("ResolveOntology named software failed: %v", err)
	}
	if yaml2 != "entities:\n  - name: App" {
		t.Errorf("unexpected yaml content from named ontology: %q", yaml2)
	}
}

func TestEnginePollJobStrictTerminalContract(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// 1. Rejects invalid non-job statuses: completed, ok, indexed, success, queue_full, rejected
	for _, invalidStatus := range []string{"completed", "ok", "indexed", "success", "queue_full", "rejected", "custom_active"} {
		statusCalls := 0
		server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
			switch name {
			case "space_info":
				return `{"status":"ok","space_id":"sp1"}`, false
			case "long_status":
				return `{"status":"ok","documents":[]}`, false
			case "long_ingest_async":
				return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-nonjob-1"}]}`, false
			case "long_ingest_status", "long_ingest_job_status":
				statusCalls++
				return fmt.Sprintf(`{"status":"%s"}`, invalidStatus), false
			default:
				return `{"status":"error","message":"unexpected tool"}`, false
			}
		})

		client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
		engine := NewEngine(client)
		engine.PollInterval = 10 * time.Millisecond

		status, err := engine.PollJob(context.Background(), "sp1", "job-nonjob-1", 500*time.Millisecond)
		server.Close()

		if statusCalls == 0 {
			t.Fatalf("expected status endpoint to be called, got 0 calls")
		}
		if err == nil {
			t.Fatalf("expected PollJob to fail for non-job status %q, got status %q", invalidStatus, status)
		}
	}
}

func TestEngineRunRejectsBatchItemWithCompletedStatus(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok","space_id":"sp1"}`, false
		case "long_document_list", "long_ingest_list", "long_status":
			return `{"status":"ok","documents":[]}`, false
		case "long_ingest_async":
			// Batch item returns non-canonical "completed" status -> must fail closed
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"completed","job_id":"job-comp-1"}]}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)

	res, err := engine.Run(context.Background(), IngestOptions{
		Path:    tempDir,
		SpaceID: "sp1",
	}, nil)

	if err != nil {
		t.Fatalf("unexpected engine run error: %v", err)
	}
	if res.TotalFailed != 1 || res.TotalSucceeded != 0 {
		t.Fatalf("expected batch item with status:completed to fail closed (TotalFailed=1, TotalSucceeded=0), got %+v", res)
	}
}

func TestEnginePollJobRejectsConflictingStatusStateOverride(t *testing.T) {
	statusCalls := 0
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "long_ingest_status", "long_ingest_job_status":
			statusCalls++
			// status is "running" (non-terminal), state is "succeeded" -> must NOT treat as terminal success
			return `{"status":"running","state":"succeeded"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	engine := NewEngine(client)
	engine.PollInterval = 10 * time.Millisecond

	status, err := engine.PollJob(context.Background(), "sp1", "job-conflict-1", 200*time.Millisecond)
	if statusCalls == 0 {
		t.Fatalf("expected status endpoint to be called, got 0 calls")
	}
	if err == nil {
		t.Fatalf("expected PollJob to fail/timeout when status is running (despite state:succeeded), got status %q", status)
	}
}

func TestEngineSubmissionOutcome(t *testing.T) {
	cases := []struct {
		name                       string
		statuses                   []string
		watch, missingID, dryRun   bool
		pollStatus                 string
		succeeded, pending, failed int
		skipped, uploaded, polls   int
	}{
		{name: "queued", statuses: []string{"queued"}, pending: 1, uploaded: 1},
		{name: "running", statuses: []string{"running"}, pending: 1, uploaded: 1},
		{name: "processing", statuses: []string{"processing"}, pending: 1, uploaded: 1},
		{name: "pending", statuses: []string{"pending"}, pending: 1, uploaded: 1},
		{name: "in_progress", statuses: []string{"in_progress"}, pending: 1, uploaded: 1},
		{name: "case_preserved", statuses: []string{"RUNNING"}, pending: 1, uploaded: 1},
		{name: "immediate_success_once", statuses: []string{"succeeded"}, succeeded: 1, uploaded: 1},
		{name: "skipped", statuses: []string{"skipped"}, skipped: 1},
		{name: "changed_skipped", statuses: []string{"changed_skipped"}, skipped: 1},
		{name: "failed", statuses: []string{"failed"}, failed: 1},
		{name: "mixed", statuses: []string{"queued", "succeeded", "failed", "skipped"}, succeeded: 1, pending: 1, failed: 1, skipped: 1, uploaded: 2},
		{name: "missing_job_id", statuses: []string{"pending"}, missingID: true, failed: 1},
		{name: "unknown_status", statuses: []string{"custom_active"}, failed: 1},
		{name: "watched_success", statuses: []string{"queued"}, watch: true, pollStatus: "succeeded", succeeded: 1, uploaded: 1, polls: 1},
		{name: "watched_failure", statuses: []string{"queued"}, watch: true, pollStatus: "failed", failed: 1, uploaded: 1, polls: 1},
		{name: "watched_skip", statuses: []string{"queued"}, watch: true, pollStatus: "skipped", skipped: 1, uploaded: 1, polls: 1},
		{name: "watched_immediate_success_once", statuses: []string{"succeeded"}, watch: true, succeeded: 1, uploaded: 1},
		{name: "dry_run_offline", statuses: []string{"queued"}, dryRun: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			items := make([]map[string]interface{}, 0, len(tc.statuses))
			for i, status := range tc.statuses {
				filename := fmt.Sprintf("doc-%d.md", i)
				if err := os.WriteFile(filepath.Join(dir, filename), []byte("# Synthetic document"), 0600); err != nil {
					t.Fatal(err)
				}
				jobID := fmt.Sprintf("job-%d", i)
				if tc.missingID {
					jobID = ""
				}
				items = append(items, map[string]interface{}{"source_path": filename, "status": status, "job_id": jobID})
			}
			calls, polls := 0, 0
			server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				calls++
				switch name {
				case "space_info":
					return `{"status":"ok"}`, false
				case "long_document_list", "long_ingest_list":
					return `{"status":"ok","documents":[],"jobs":[]}`, false
				case "long_ingest_async":
					body, err := json.Marshal(map[string]interface{}{"status": "ok", "items": items})
					if err != nil {
						t.Error(err)
					}
					return string(body), false
				case "long_ingest_status", "long_ingest_job_status":
					polls++
					return fmt.Sprintf(`{"status":%q}`, tc.pollStatus), false
				default:
					t.Errorf("unexpected tool %s", name)
					return `{"status":"error"}`, true
				}
			})
			defer server.Close()
			engine := NewEngine(mcpclient.NewClient(server.URL, "synthetic-token", time.Second))
			engine.PollInterval = time.Millisecond
			var finalStage, finalMessage, finalStatus string
			res, err := engine.Run(context.Background(), IngestOptions{
				Path: dir, SpaceID: "synthetic-space", WatchJobs: tc.watch, DryRun: tc.dryRun, Timeout: time.Second,
			}, func(stage, message string, current, total int, file, jobID, status, errStr string) {
				finalStage, finalMessage, finalStatus = stage, message, status
			})
			if err != nil {
				t.Fatal(err)
			}
			if polls != tc.polls || (tc.dryRun && calls != 0) {
				t.Errorf("calls=%d polls=%d, wanted polls=%d (dry_run=%t)", calls, polls, tc.polls, tc.dryRun)
			}
			wantSuccess := tc.dryRun || (tc.pending == 0 && tc.failed == 0)
			if res.Success != wantSuccess || res.TotalSucceeded != tc.succeeded || res.TotalFailed != tc.failed || res.TotalSkipped != tc.skipped || res.TotalUploaded != tc.uploaded || res.TotalScanned != len(tc.statuses) {
				t.Errorf("incorrect submission accounting: %+v; want success=%t succeeded=%d failed=%d skipped=%d uploaded=%d", res, wantSuccess, tc.succeeded, tc.failed, tc.skipped, tc.uploaded)
			}
			// Decode the public JSON field so this regression test also runs before the new field exists.
			encoded, err := json.Marshal(res)
			if err != nil {
				t.Fatal(err)
			}
			var outcome struct {
				TotalPending *int `json:"total_pending"`
			}
			if err := json.Unmarshal(encoded, &outcome); err != nil {
				t.Fatal(err)
			}
			if outcome.TotalPending == nil || *outcome.TotalPending != tc.pending {
				t.Errorf("JSON must expose total_pending=%d: %s", tc.pending, encoded)
			}
			if len(res.Jobs) != len(tc.statuses) {
				t.Fatalf("wrong job count: %d", len(res.Jobs))
			}
			if tc.pending > 0 {
				if finalStage != "submitted" || finalStatus != "pending" || !strings.HasPrefix(finalMessage, "Submitted") || strings.Contains(strings.ToLower(finalMessage), "completed") {
					t.Errorf("pending callback falsely signals completion: %q / %q / %q", finalStage, finalStatus, finalMessage)
				}
				if res.Jobs[0].Status != tc.statuses[0] || res.Jobs[0].JobID != "job-0" {
					t.Errorf("pending job identity/status lost: %+v", res.Jobs[0])
				}
			} else if tc.dryRun && finalStage != "dry_run" {
				t.Errorf("dry-run callback changed: %s", finalStage)
			}
		})
	}
}

func mustJSON(s string) []byte {
	b, _ := json.Marshal(s)
	return b
}
