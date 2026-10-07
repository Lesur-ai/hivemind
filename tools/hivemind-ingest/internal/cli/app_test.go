package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"hivemind-ingest/internal/config"
	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/tui"
)

func TestAppDefaultsToV160(t *testing.T) {
	var in bytes.Buffer
	var out bytes.Buffer
	ui := tui.NewUI(&in, &out)

	for name, app := range map[string]*App{
		"constructor":      NewApp(ui, &config.Config{}),
		"empty build info": NewAppWithBuildInfo(ui, &config.Config{}, "", "", ""),
	} {
		t.Run(name, func(t *testing.T) {
			if app.version != "v1.6.0" {
				t.Fatalf("expected v1.6.0 fallback, got %q", app.version)
			}
		})
	}
}

func TestAppCLIEndToEnd(t *testing.T) {
	tempDir := t.TempDir()
	docFile := filepath.Join(tempDir, "manual.md")
	_ = os.WriteFile(docFile, []byte("# Project Manual\nArchitecture guide"), 0644)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req mcpclient.JSONRPCRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
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

		params, _ := req.Params.(map[string]interface{})
		name, _ := params["name"].(string)

		var resBody string
		switch name {
		case "system_whoami":
			resBody = `{"status":"ok","user":"ci-runner"}`
		case "space_info":
			resBody = `{"status":"ok","space_id":"test-space"}`
		case "space_create":
			resBody = `{"status":"created"}`
		case "space_delete":
			resBody = `{"status":"ok"}`
		case "long_ingest_list", "long_document_list":
			resBody = `{"status":"ok","documents":[]}`
		case "long_ingest_async", "long_ingest_document":
			resBody = `{"status":"ok","batch_id":"batch-1","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"manual.md","job_id":"job-test-1","status":"queued"}],"errors":[]}`
		case "long_ingest_status", "long_ingest_job_status":
			resBody = `{"status":"succeeded","job_id":"job-test-1"}`
		case "ontology_get":
			resBody = `{"status":"ok","content_yaml":"entities:\n  - name: Document\n  - name: Concept"}`
		case "ontology_validate":
			resBody = `{"status":"ok","valid":true}`
		case "long_status":
			resBody = `{"status":"ok","graph_stats":{"entities_count":2,"entity_types":{"Document":1,"Concept":1}}}`
		case "long_test_ontology":
			resBody = `{"status":"ok","entities":[{"name":"Project Manual","type":"Document"},{"name":"Architecture","type":"Concept"}],"relations":[]}`
		default:
			resBody = `{"status":"ok"}`
		}

		resp := mcpclient.JSONRPCResponse{
			JSONRPC: "2.0",
			ID:      req.ID,
			Result:  json.RawMessage(`{"content":[{"type":"text","text":` + string(mustJSON(resBody)) + `}],"isError":false}`),
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer server.Close()

	cfg := &config.Config{
		Endpoint:          server.URL,
		Token:             "ci-token",
		SpaceID:           "test-space",
		BatchSizeMB:       50,
		TimeoutSeconds:    10,
		ThresholdOther:    25,
		AllowedExtensions: []string{".md"},
	}

	var in bytes.Buffer
	var out bytes.Buffer
	ui := tui.NewUI(&in, &out)
	app := NewApp(ui, cfg)

	// 1. Test config test (code 0)
	out.Reset()
	code := app.Run(context.Background(), []string{"config", "test", "--json"})
	if code != ExitSuccess {
		t.Fatalf("config test failed with exit code %d, output: %s", code, out.String())
	}
	if !strings.Contains(out.String(), "ci-runner") {
		t.Errorf("expected output to contain ci-runner, got %s", out.String())
	}

	// 2. Test test-ontology (code 0)
	out.Reset()
	code = app.Run(context.Background(), []string{"test-ontology", "--path", tempDir, "--ontology", "software", "--json"})
	if code != ExitSuccess {
		t.Fatalf("test-ontology failed with exit code %d, output: %s", code, out.String())
	}
	if !strings.Contains(out.String(), `"passed_threshold": true`) {
		t.Errorf("expected passed_threshold true, got %s", out.String())
	}

	// 3. Test run dry-run (code 0)
	out.Reset()
	code = app.Run(context.Background(), []string{"run", "--path", tempDir, "--space", "test-space", "--dry-run", "--json"})
	if code != ExitSuccess {
		t.Fatalf("run --dry-run failed with code %d, output: %s", code, out.String())
	}
	if !strings.Contains(out.String(), `"total_scanned": 1`) {
		t.Errorf("expected total_scanned 1, got %s", out.String())
	}

	// 4. Test run real ingest with timeout flag (code 0)
	out.Reset()
	code = app.Run(context.Background(), []string{"run", "--path", tempDir, "--space", "test-space", "--timeout", "5", "--json"})
	if code != ExitSuccess {
		t.Fatalf("run failed with code %d, output: %s", code, out.String())
	}
	if !strings.Contains(out.String(), `"total_succeeded": 1`) {
		t.Errorf("expected total_succeeded 1, got %s", out.String())
	}

	// 5. Test validation error (code 1)
	out.Reset()
	code = app.Run(context.Background(), []string{"run", "--invalid-flag-123"})
	if code != ExitValidationError {
		t.Errorf("expected ExitValidationError (1) on invalid flag, got %d", code)
	}

	// 6. Test network error (code 2)
	out.Reset()
	code = app.Run(context.Background(), []string{"run", "--path", tempDir, "--space", "test-space", "--endpoint", "http://127.0.0.1:54321/down", "--non-interactive", "--json"})
	if code != ExitNetworkError {
		t.Errorf("expected ExitNetworkError (2) on network failure, got %d", code)
	}
}

func TestAppSubmissionOutcome(t *testing.T) {
	for _, mode := range []struct {
		name          string
		flags         []string
		watch, dryRun bool
	}{
		{name: "no_poll", flags: []string{"--no-poll"}},
		{name: "watch_false", flags: []string{"--watch=false"}},
		{name: "no_poll_overrides_watch", flags: []string{"--no-poll", "--watch=true"}},
		{name: "default_watch", watch: true},
		{name: "dry_run", flags: []string{"--dry-run", "--no-poll"}, dryRun: true},
	} {
		for _, statuses := range [][]string{
			{"queued"}, {"running"}, {"pending"}, {"processing"}, {"in_progress"},
			{"succeeded"}, {"skipped"}, {"failed"}, {"queued", "failed"},
		} {
			// Polling is already covered per status by the engine; keep CLI watch/dry-run parity focused.
			if (mode.watch || mode.dryRun) && len(statuses) == 1 && statuses[0] != "queued" && statuses[0] != "failed" {
				continue
			}
			for _, jsonOutput := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/%s/json=%t", mode.name, strings.Join(statuses, "+"), jsonOutput), func(t *testing.T) {
					dir := t.TempDir()
					items := make([]map[string]interface{}, 0, len(statuses))
					pending, succeeded, skipped, failed, wantPolls := 0, 0, 0, 0, 0
					for i, status := range statuses {
						filename := fmt.Sprintf("doc-%d.md", i)
						if err := os.WriteFile(filepath.Join(dir, filename), []byte("# Synthetic document"), 0600); err != nil {
							t.Fatal(err)
						}
						items = append(items, map[string]interface{}{"source_path": filename, "job_id": fmt.Sprintf("job-%d", i), "status": status})
						if mode.dryRun {
							continue
						}
						switch status {
						case "succeeded":
							succeeded++
						case "skipped":
							skipped++
						case "failed":
							failed++
						default:
							if mode.watch {
								succeeded++
								wantPolls++
							} else {
								pending++
							}
						}
					}
					calls, polls := 0, 0
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						calls++
						var req mcpclient.JSONRPCRequest
						if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
							t.Error(err)
							http.Error(w, "bad request", 400)
							return
						}
						w.Header().Set("Content-Type", "application/json")
						var result interface{}
						switch req.Method {
						case "initialize":
							result = map[string]interface{}{"protocolVersion": "2024-11-05", "capabilities": map[string]interface{}{"tools": map[string]interface{}{}}, "serverInfo": map[string]string{"name": "synthetic", "version": "1"}}
						case "notifications/initialized":
							return
						case "tools/call":
							params, _ := req.Params.(map[string]interface{})
							var body interface{}
							switch params["name"] {
							case "space_info":
								body = map[string]string{"status": "ok"}
							case "long_document_list", "long_ingest_list":
								body = map[string]interface{}{"status": "ok", "documents": []interface{}{}, "jobs": []interface{}{}}
							case "long_ingest_async":
								body = map[string]interface{}{"status": "ok", "items": items}
							case "long_ingest_status", "long_ingest_job_status":
								polls++
								body = map[string]string{"status": "succeeded"}
							default:
								t.Errorf("unexpected tool: %v", params["name"])
								body = map[string]string{"status": "error"}
							}
							encoded, err := json.Marshal(body)
							if err != nil {
								t.Error(err)
							}
							result = map[string]interface{}{"content": []interface{}{map[string]string{"type": "text", "text": string(encoded)}}, "isError": false}
						default:
							t.Errorf("unexpected method: %s", req.Method)
						}
						if err := json.NewEncoder(w).Encode(map[string]interface{}{"jsonrpc": "2.0", "id": req.ID, "result": result}); err != nil {
							t.Error(err)
						}
					}))
					defer server.Close()
					var out bytes.Buffer
					app := NewApp(tui.NewUI(strings.NewReader(""), &out), &config.Config{Endpoint: server.URL, SpaceID: "synthetic-space", BatchSizeMB: 50, TimeoutSeconds: 5})
					args := append([]string{"run", "--path", dir, "--non-interactive"}, mode.flags...)
					if jsonOutput {
						args = append(args, "--json")
					}
					// handleRun's final summary is written directly to os.Stdout, while progress uses ui.out.
					capture, err := os.CreateTemp(t.TempDir(), "stdout-")
					if err != nil {
						t.Fatal(err)
					}
					defer capture.Close()
					originalStdout := os.Stdout
					os.Stdout = capture
					defer func() { os.Stdout = originalStdout }()
					code := app.Run(context.Background(), args)
					os.Stdout = originalStdout
					if _, err := capture.Seek(0, 0); err != nil {
						t.Fatal(err)
					}
					stdout, err := io.ReadAll(capture)
					if err != nil {
						t.Fatal(err)
					}
					wantCode := 0
					if failed > 0 {
						wantCode = 3
					} else if pending > 0 {
						wantCode = 4
					}
					if code != wantCode {
						t.Errorf("exit=%d, want %d; output=%s%s", code, wantCode, stdout, out.String())
					}
					if polls != wantPolls || (mode.dryRun && calls != 0) {
						t.Errorf("calls=%d polls=%d, want polls=%d dryRun=%t", calls, polls, wantPolls, mode.dryRun)
					}
					if jsonOutput {
						var result struct {
							Success   bool `json:"success"`
							Pending   *int `json:"total_pending"`
							Succeeded int  `json:"total_succeeded"`
							Failed    int  `json:"total_failed"`
							Skipped   int  `json:"total_skipped"`
							Jobs      []struct {
								JobID  string `json:"job_id"`
								Status string `json:"status"`
							} `json:"jobs"`
						}
						if err := json.Unmarshal(out.Bytes(), &result); err != nil {
							t.Fatal(err)
						}
						if len(stdout) != 0 {
							t.Errorf("JSON mode leaked text: %s", stdout)
						}
						if result.Success != (wantCode == 0) || result.Pending == nil || *result.Pending != pending || result.Succeeded != succeeded || result.Failed != failed || result.Skipped != skipped {
							t.Errorf("incorrect JSON outcome: %s", out.String())
						}
						if pending > 0 && (len(result.Jobs) != len(statuses) || result.Jobs[0].JobID != "job-0" || result.Jobs[0].Status != statuses[0]) {
							t.Errorf("pending identity/status missing: %s", out.String())
						}
					} else if pending > 0 {
						combined := strings.ToLower(string(stdout) + out.String())
						if strings.Contains(combined, "completed") || !strings.Contains(combined, "submitted") || !strings.Contains(combined, "pending") {
							t.Errorf("pending output falsely signals completion: %s", combined)
						}
						if !strings.Contains(out.String(), "Submitted") {
							t.Errorf("progress callback missing submitted state: %s", out.String())
						}
					} else if wantCode == 0 && !strings.Contains(string(stdout), "completed successfully") {
						t.Errorf("successful output changed: %s", stdout)
					}
				})
			}
		}
	}
}

func mustJSON(s string) []byte {
	b, _ := json.Marshal(s)
	return b
}

func TestAutomaticNoPollIsValidationError(t *testing.T) {
	for _, flag := range []string{"--no-poll", "--watch=false"} {
		t.Run(flag, func(t *testing.T) {
			cfg := config.DefaultConfig()
			cfg.Endpoint = "http://127.0.0.1:1/mcp"
			app := NewApp(tui.NewUI(strings.NewReader(""), io.Discard), cfg)
			code := app.Run(context.Background(), []string{"run", "--path", t.TempDir(), "--space", "bulk", "--ontology", "auto", flag, "--non-interactive", "--json"})
			if code != ExitValidationError {
				t.Fatalf("expected validation error before server access, got%d", code)
			}
		})
	}
}

func TestAutomaticEmptySelectionIsValidationError(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "empty.md"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	cfg := config.DefaultConfig()
	cfg.Endpoint = "http://127.0.0.1:1/mcp"
	var out bytes.Buffer
	app := NewApp(tui.NewUI(strings.NewReader(""), &out), cfg)
	code := app.Run(context.Background(), []string{"run", "--path", dir, "--space", "bulk", "--ontology", "auto", "--dry-run", "--non-interactive", "--json"})
	if code != ExitValidationError || !strings.Contains(out.String(), "at least one non-empty source") {
		t.Fatalf("empty selection dry-run must fail validation, got code=%d output=%s", code, out.String())
	}
}

func TestAutomaticBootstrapFailureHumanSummaryIncludesNotSubmitted(t *testing.T) {
	dir := t.TempDir()
	for i := 0; i < 13; i++ {
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("doc-%02d.md", i)), []byte(fmt.Sprintf("# Synthetic document %d", i)), 0600); err != nil {
			t.Fatal(err)
		}
	}
	submitted := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req mcpclient.JSONRPCRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Error(err)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		var result interface{}
		switch req.Method {
		case "initialize":
			result = map[string]interface{}{"protocolVersion": "2024-11-05", "capabilities": map[string]interface{}{"tools": map[string]interface{}{}}, "serverInfo": map[string]string{"name": "synthetic", "version": "1"}}
		case "notifications/initialized":
			return
		case "tools/call":
			params := req.Params.(map[string]interface{})
			var body interface{}
			switch params["name"] {
			case "space_info":
				body = map[string]string{"status": "ok"}
			case "long_document_list", "long_ingest_list":
				body = map[string]interface{}{"status": "ok", "documents": []interface{}{}, "jobs": []interface{}{}}
			case "long_ingest_async":
				args := params["arguments"].(map[string]interface{})
				documents := args["documents"].([]interface{})
				submitted += len(documents)
				items := make([]map[string]interface{}, 0, len(documents))
				for i, document := range documents {
					items = append(items, map[string]interface{}{"source_path": document.(map[string]interface{})["source_path"], "job_id": fmt.Sprintf("job-%d", i), "status": "queued"})
				}
				body = map[string]interface{}{"status": "ok", "items": items}
			case "long_ingest_status":
				body = map[string]string{"status": "failed", "error": "automatic_ontology_document_has_no_text"}
			default:
				t.Errorf("unexpected tool: %v", params["name"])
				body = map[string]string{"status": "error"}
			}
			encoded, _ := json.Marshal(body)
			result = map[string]interface{}{"content": []interface{}{map[string]string{"type": "text", "text": string(encoded)}}, "isError": false}
		}
		_ = json.NewEncoder(w).Encode(map[string]interface{}{"jsonrpc": "2.0", "id": req.ID, "result": result})
	}))
	defer server.Close()
	cfg := config.DefaultConfig()
	cfg.Endpoint = server.URL
	app := NewApp(tui.NewUI(strings.NewReader(""), io.Discard), cfg)
	capture, err := os.CreateTemp(t.TempDir(), "stdout-")
	if err != nil {
		t.Fatal(err)
	}
	defer capture.Close()
	originalStdout := os.Stdout
	os.Stdout = capture
	defer func() { os.Stdout = originalStdout }()
	code := app.Run(context.Background(), []string{"run", "--path", dir, "--space", "bulk", "--ontology", "auto", "--non-interactive"})
	os.Stdout = originalStdout
	if _, err := capture.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	stdout, err := io.ReadAll(capture)
	if err != nil {
		t.Fatal(err)
	}
	if submitted != 12 || code != ExitIngestionFailure || !strings.Contains(string(stdout), "0 succeeded, 12 failed, 1 not submitted") {
		t.Fatalf("bootstrap failure must retain the whole corpus outcome: submitted=%d code=%d summary=%s", submitted, code, stdout)
	}
}
