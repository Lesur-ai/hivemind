package ingest

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"hivemind-ingest/internal/mcpclient"
)

const unboundCatalogError = `{"status":"error","message":"Space 'fresh' is not connected to Graph Memory. Use graph_connect first."}`
const unboundEmbeddedStatus = `{"status":"ok","space_id":"fresh","connected":false,"bound":false,"embedded":true,"mid_archive_projection":{"pending":0,"error":null}}`

func TestFreshEmbeddedSpaceReachesFirstIngest(t *testing.T) {
	for _, ontology := range []string{"", "general", "auto"} {
		t.Run("ontology="+ontology, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.WriteFile(filepath.Join(dir, "source.md"), []byte("A complete source."), 0600); err != nil {
				t.Fatal(err)
			}
			var checked, uploaded atomic.Bool
			srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				switch name {
				case "space_info":
					return `{"status":"ok","space_id":"fresh"}`, false
				case "long_ingest_list":
					return `{"status":"ok","jobs":[]}`, false
				case "long_document_list":
					return unboundCatalogError, false
				case "long_status":
					checked.Store(true)
					return unboundEmbeddedStatus, false
				case "ontology_get":
					return `{"status":"ok","content_yaml":"name: general"}`, false
				case "ontology_validate":
					return `{"status":"ok","valid":true}`, false
				case "long_ingest_async":
					if !checked.Load() {
						t.Error("ingestion began without positive binding evidence")
					}
					got := args["options"].(map[string]interface{})["ontology"]
					if ontology == "auto" && got != "auto" {
						t.Errorf("lost automatic bootstrap: %v", got)
					}
					if ontology == "general" && got != "name: general" {
						t.Errorf("lost chosen ontology: %v", got)
					}
					uploaded.Store(true)
					return `{"status":"ok","items":[{"source_path":"source.md","job_id":"first","status":"queued"}]}`, false
				case "long_ingest_status":
					return `{"status":"succeeded"}`, false
				default:
					t.Errorf("unexpected tool %s", name)
					return `{"status":"error"}`, false
				}
			})
			defer srv.Close()
			e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
			e.PollInterval = time.Millisecond
			result, err := e.Run(context.Background(), IngestOptions{Path: dir, SpaceID: "fresh", Ontology: ontology, WatchJobs: true, Timeout: time.Second}, nil)
			if err != nil {
				t.Fatalf("fresh embedded ingestion blocked: %v", err)
			}
			if !uploaded.Load() || !result.Success || result.TotalSucceeded != 1 {
				t.Fatalf("first ingestion not completed: %+v", result)
			}
		})
	}
}

func TestUnboundCatalogRequiresExactSafeStatus(t *testing.T) {
	cases := []struct {
		name, key string
		value     interface{}
		remove    bool
	}{
		{"wrong_space", "space_id", "other", false}, {"missing_space", "space_id", nil, true},
		{"error_status", "status", "error", false}, {"recovery_status", "status", "recovery_required", false}, {"missing_status", "status", nil, true},
		{"connected", "connected", true, false}, {"missing_connected", "connected", nil, true}, {"string_connected", "connected", "false", false},
		{"bound", "bound", true, false}, {"missing_bound", "bound", nil, true}, {"null_bound", "bound", nil, false}, {"string_bound", "bound", "false", false},
		{"remote", "embedded", false, false}, {"missing_embedded", "embedded", nil, true}, {"string_embedded", "embedded", "true", false},
		{"tool_error", "isError", true, false}, {"malformed_tool_error", "isError", "false", false},
		{"error_marker", "error", "unavailable", false}, {"recovery_marker", "recovery_required", true, false}, {"recovery_code", "code", "recovery_required", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			status := map[string]interface{}{}
			if err := json.Unmarshal([]byte(unboundEmbeddedStatus), &status); err != nil {
				t.Fatal(err)
			}
			if tc.remove {
				delete(status, tc.key)
			} else {
				status[tc.key] = tc.value
			}
			body, _ := json.Marshal(status)
			var probes atomic.Int32
			srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				if name == "long_status" {
					probes.Add(1)
					return string(body), false
				}
				return unboundCatalogError, false
			})
			defer srv.Close()
			e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
			known, err := e.FetchKnownDocuments(context.Background(), "fresh")
			if err == nil || known != nil {
				t.Fatalf("unsafe status treated as empty catalog: %+v / %v", known, err)
			}
			if probes.Load() != 1 {
				t.Fatalf("want one status check, got %d", probes.Load())
			}
		})
	}
}

func TestCatalogOtherErrorsDoNotProbeOrBecomeEmpty(t *testing.T) {
	for _, msg := range []string{"Permission denied", "document_list error: HTTP 401", "document_list error: connection refused", "Space 'other' is not connected to Graph Memory. Use graph_connect first."} {
		t.Run(msg, func(t *testing.T) {
			srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				if name == "long_status" {
					t.Error("unrelated catalog error triggered status fallback")
					return unboundEmbeddedStatus, false
				}
				body, _ := json.Marshal(map[string]interface{}{"status": "error", "message": msg})
				return string(body), false
			})
			defer srv.Close()
			_, err := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second)).FetchKnownDocuments(context.Background(), "fresh")
			if err == nil || !strings.Contains(err.Error(), msg) {
				t.Fatalf("lost catalog error: %v", err)
			}
		})
	}
}

func TestCatalogTransportFailureCannotBecomeEmpty(t *testing.T) {
	for _, code := range []int{http.StatusUnauthorized, http.StatusForbidden, http.StatusServiceUnavailable} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			backend := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				if name == "long_status" {
					t.Error("transport failure triggered status fallback")
				}
				return unboundEmbeddedStatus, false
			})
			defer backend.Close()
			// Retain the real handshake handler; reject catalog calls at HTTP level.
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var req mcpclient.JSONRPCRequest
				data := json.NewDecoder(r.Body)
				if err := data.Decode(&req); err != nil {
					t.Error(err)
					return
				}
				if req.Method == "tools/call" {
					http.Error(w, "not connected", code)
					return
				}
				body, _ := json.Marshal(req)
				r.Body = http.NoBody
				r.Body = io.NopCloser(strings.NewReader(string(body)))
				backend.Config.Handler.ServeHTTP(w, r)
			}))
			defer server.Close()
			_, err := NewEngine(mcpclient.NewClient(server.URL, "token", time.Second)).FetchKnownDocuments(context.Background(), "fresh")
			if err == nil || !strings.Contains(err.Error(), fmt.Sprint(code)) {
				t.Fatalf("transport error swallowed: %v", err)
			}
		})
	}
}

func TestPartialCatalogNeverFallsBackToUnbound(t *testing.T) {
	var pages atomic.Int32
	srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		if name == "long_status" {
			t.Error("partial catalog must not use empty fallback")
			return unboundEmbeddedStatus, false
		}
		if pages.Add(1) > 1 {
			return unboundCatalogError, false
		}
		docs := make([]map[string]interface{}, 100)
		for i := range docs {
			docs[i] = map[string]interface{}{"source_path": fmt.Sprint(i), "sha256": strings.Repeat("a", 64), "ingestion_status": "succeeded"}
		}
		body, _ := json.Marshal(map[string]interface{}{"status": "ok", "documents": docs})
		return string(body), false
	})
	defer srv.Close()
	known, err := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second)).FetchKnownDocuments(context.Background(), "fresh")
	if err == nil || known != nil || pages.Load() != 2 {
		t.Fatalf("partial catalog accepted: known=%v err=%v pages=%d", known, err, pages.Load())
	}
}

func TestUnboundCatalogStatusFailurePreservesOriginalError(t *testing.T) {
	for _, response := range []struct {
		name, body string
		isError    bool
	}{
		{"status_tool_error", unboundEmbeddedStatus, true},
		{"status_denied", `{"status":"error","message":"Permission denied"}`, false},
		{"malformed_status", `unavailable`, false},
	} {
		t.Run(response.name, func(t *testing.T) {
			srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				if name == "long_status" {
					return response.body, response.isError
				}
				return unboundCatalogError, false
			})
			defer srv.Close()
			known, err := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second)).FetchKnownDocuments(context.Background(), "fresh")
			if err == nil || known != nil || !strings.Contains(err.Error(), "Space 'fresh' is not connected") {
				t.Fatalf("ambiguous status swallowed catalog error: %v %v", known, err)
			}
		})
	}
}
