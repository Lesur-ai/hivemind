package ontology

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

func TestEvaluator(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Software Spec\nServer and Database"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-eval-1","total":1,"counts":{"queued":1},"items":[{"index":0,"source_path":"doc.md","job_id":"job-123","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"entities_count":3,"entity_types":{"Component":1,"Database":1,"Other":1}},"top_entities":[{"name":"Misc","type":"Other"}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	opts := EvalOptions{
		Ontology:       "entities:\n  - name: Component",
		SampleSize:     1,
		ThresholdOther: 40, // 1 Other out of 3 = 33.3% -> passes
	}

	res, err := eval.TestOntology(context.Background(), tempDir, opts)
	if err != nil {
		t.Fatalf("TestOntology failed: %v", err)
	}

	if !res.PassedThreshold {
		t.Errorf("expected PassedThreshold true, got false")
	}
	if res.TotalEntities != 3 {
		t.Errorf("expected 3 entities, got %d", res.TotalEntities)
	}
	if res.OtherEntities != 1 {
		t.Errorf("expected 1 other entity, got %d", res.OtherEntities)
	}
	if len(res.UnclassifiedConcepts) != 1 || res.UnclassifiedConcepts[0].Name != "Misc" {
		t.Errorf("expected 1 unclassified concept 'Misc', got %+v", res.UnclassifiedConcepts)
	}

	// Test failing threshold
	opts.ThresholdOther = 20 // 33.3% > 20% -> fails
	res2, err := eval.TestOntology(context.Background(), tempDir, opts)
	if err != nil {
		t.Fatalf("TestOntology2 failed: %v", err)
	}
	if res2.PassedThreshold {
		t.Errorf("expected PassedThreshold false, got true")
	}
}

func TestEvaluatorFailClosedOnServerError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Content"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		return `{"status":"error","message":"LLM rate limit reached"}`, false
	})
	defer server.Close()

	client := NewEvaluator(mcpclient.NewClient(server.URL, "tok", 10*time.Second))
	_, err := client.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Server",
		SampleSize:     1,
		ThresholdOther: 25,
	})
	if err == nil {
		t.Fatal("expected TestOntology to fail closed on server error, got nil")
	}
}

func TestEvaluatorRejectsInvalidYAML(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Content"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		return `{"status":"ok","valid":false,"errors":["missing entities section"]}`, false
	})
	defer server.Close()

	client := NewEvaluator(mcpclient.NewClient(server.URL, "tok", 10*time.Second))
	_, err := client.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "invalid-yaml",
		SampleSize:     1,
		ThresholdOther: 25,
	})
	if err == nil {
		t.Fatal("expected TestOntology to fail closed on invalid YAML, got nil")
	}
}

func TestEvaluatorRejectsInvalidThreshold(t *testing.T) {
	client := NewEvaluator(mcpclient.NewClient("http://mock", "tok", 10*time.Second))
	_, err := client.TestOntology(context.Background(), ".", EvalOptions{
		ThresholdOther: 150, // Invalid > 100
	})
	if err == nil {
		t.Fatal("expected error on threshold > 100, got nil")
	}
}

func TestEvaluatorNamedOntologyResolution(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Cloud Architecture\nCluster and Nodes"), 0644)

	spaceCreated := false
	var targetSpaceSeen string
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_create":
			spaceCreated = true
			targetSpaceSeen, _ = args["space_id"].(string)
			return `{"status":"created"}`, false
		case "ontology_get":
			if !spaceCreated {
				return `{"status":"error","message":"space_create must be called before ontology_get"}`, false
			}
			spID, _ := args["space_id"].(string)
			if spID == "" || spID != targetSpaceSeen {
				return `{"status":"error","message":"ontology_get received invalid space_id"}`, false
			}
			if args["name"] != "cloud" {
				return `{"status":"error","message":"unknown ontology"}`, false
			}
			return `{"status":"ok","content_yaml":"entities:\n  - name: Cluster\n  - name: Node"}`, false
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-cloud-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			// Real production contract without artificial entity_types in graph_stats
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":2,"relation_count":1},"top_entities":[{"name":"K8s","type":"Cluster"},{"name":"Worker1","type":"Node"}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	res, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "cloud", // Named ontology
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err != nil {
		t.Fatalf("TestOntology with named ontology failed: %v", err)
	}

	if !spaceCreated {
		t.Errorf("expected space_create to be called")
	}
	if !res.PassedThreshold {
		t.Errorf("expected PassedThreshold true, got false")
	}
	if res.TotalEntities != 2 {
		t.Errorf("expected 2 entities, got %d", res.TotalEntities)
	}
	if res.OtherEntities != 0 {
		t.Errorf("expected 0 other entities, got %d", res.OtherEntities)
	}
	if res.OtherPercentage != 0.0 {
		t.Errorf("expected 0%% Other, got %f", res.OtherPercentage)
	}
}

func TestEvaluatorCancelsJobsOnCleanup(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Data"), 0644)

	cancelledJobID := ""
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-to-cancel","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"failed","error":"job stopped"}`, false
		case "long_ingest_cancel":
			cancelledJobID, _ = args["job_id"].(string)
			return `{"status":"ok"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Doc",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected error from failed job, got nil")
	}

	if cancelledJobID != "job-to-cancel" {
		t.Errorf("expected job-to-cancel to be cancelled on cleanup, got %q", cancelledJobID)
	}
}

func TestEvaluatorCancelsRunningJobsOnMixedBatchError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Data"), 0644)

	cancelledJobID := ""
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Mixed response: 1 queued item with job_id, but batch error present
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-active-queued","status":"queued"}],"errors":["item 1 failed validation"]}`, false
		case "long_ingest_cancel":
			cancelledJobID, _ = args["job_id"].(string)
			return `{"status":"ok"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Doc",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected error from batch errors, got nil")
	}

	if cancelledJobID != "job-active-queued" {
		t.Errorf("expected job-active-queued to be cancelled on cleanup despite mixed batch error, got %q", cancelledJobID)
	}
}

func TestEvaluatorFailsClosedWhenTotalEntitiesExceedsDetails(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Large Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			// Server reports 100 entities total, but top_entities only has 2 and entity_types map is omitted
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":100,"relation_count":50},"top_entities":[{"name":"E1","type":"Service"},{"name":"E2","type":"Database"}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail closed when total entities exceeds available details, got nil")
	}
}

func TestEvaluatorRejectsFractionalAndNegativeEntityCounts(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Test 1: Fractional total (1.5)
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1.5,"entity_types":{"Service":1.5}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail on fractional entity count 1.5, got nil")
	}

	// Test 2: Negative per-type count (-1)
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":2,"entity_types":{"Service":-1,"Other":3}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)

	_, err = eval2.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail on negative entity count, got nil")
	}

	// Test 3: Non-numeric type count ("abc")
	server3 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":2,"entity_types":{"Service":"abc"}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server3.Close()

	client3 := mcpclient.NewClient(server3.URL, "tok", 10*time.Second)
	eval3 := NewEvaluator(client3)

	_, err = eval3.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail on non-numeric entity count, got nil")
	}
}

func TestEvaluatorRejectsUnknownOrMissingJobStatus(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Test 1: items[] with unknown status
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Ambiguous status "unknown_status" without job_id
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","status":"unknown_status"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail closed on unknown ingestion status in items, got nil")
	}

	// Test 2: top-level envelope with unknown status and job_id
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"unknown_envelope_status","job_id":"job-top-1"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)

	_, err = eval2.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail closed on unknown top-level status, got nil")
	}
}

func TestEvaluatorAcceptsCanonicalTerminalStatesInPolling(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Polling returns "succeeded" (canonical terminal success state)
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1,"entity_types":{"Service":1}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	res, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err != nil {
		t.Fatalf("expected TestOntology to succeed with 'succeeded' terminal status: %v", err)
	}
	if !res.PassedThreshold {
		t.Errorf("expected PassedThreshold true, got false")
	}
}

func TestEvaluatorFailsClosedOnSkippedOrChangedSkippedStatus(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Test 1: items[] with "skipped"
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","status":"skipped"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server1.Close()
	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)
	if _, err := eval1.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on items[] skipped status, got nil")
	}

	// Test 2: jobs[] with "changed_skipped"
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-2","total":1,"jobs":[{"index":0,"source_path":"doc.md","status":"changed_skipped"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()
	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)
	if _, err := eval2.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on jobs[] changed_skipped status, got nil")
	}

	// Test 3: top-level envelope with "skipped"
	server3 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"skipped","batch_id":"batch-3","job_id":"job-skip"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server3.Close()
	client3 := mcpclient.NewClient(server3.URL, "tok", 10*time.Second)
	eval3 := NewEvaluator(client3)
	if _, err := eval3.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on top-level skipped envelope, got nil")
	}

	// Test 4: polling returns "skipped"
	server4 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-4","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-poll-skip","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"skipped"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server4.Close()
	client4 := mcpclient.NewClient(server4.URL, "tok", 10*time.Second)
	eval4 := NewEvaluator(client4)
	if _, err := eval4.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on pollJob returning skipped, got nil")
	}
}

func TestEvaluatorRejectsNonEmptyErrorsListDirectly(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-err","total":1,"items":[],"errors":[{"message":"disk full"}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	if _, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on non-empty errors list, got nil")
	}
}

func TestEvaluatorRejectsUnacknowledgedSamplesDirectly(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Acknowledges unk_other.md, but NOT doc.md
			return `{"status":"ok","batch_id":"batch-unack","total":1,"items":[{"source_path":"other.md","status":"succeeded"}],"errors":[]}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1,"entity_types":{"Service":1}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on unacknowledged sample item, got nil")
	}
	if !strings.Contains(err.Error(), "was not acknowledged") && !strings.Contains(err.Error(), "unrecognized sample") {
		t.Fatalf("expected error regarding unacknowledged sample, got: %v", err)
	}
}

func TestEvaluatorRejectsConflictingIndexAndPathAcknowledgement(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Conflicting: index 0 provided with mismatched source_path other.md
			return `{"status":"ok","batch_id":"batch-conflict","total":1,"items":[{"index":0,"source_path":"other.md","status":"succeeded"}],"errors":[]}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1,"entity_types":{"Service":1}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on conflicting index and path acknowledgement, got nil")
	}
	if !strings.Contains(err.Error(), "conflicting acknowledgement") {
		t.Fatalf("expected conflicting acknowledgement error, got: %v", err)
	}
}

func TestEvaluatorRejectsDuplicateClaimAcknowledgement(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Same sample acknowledged twice
			return `{"status":"ok","batch_id":"batch-dup","total":1,"items":[{"index":0,"status":"succeeded"},{"index":0,"status":"succeeded"}],"errors":[]}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1,"entity_types":{"Service":1}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on duplicate claim acknowledgement, got nil")
	}
	if !strings.Contains(err.Error(), "acknowledged multiple times") {
		t.Fatalf("expected duplicate acknowledgement error, got: %v", err)
	}
}

func TestEvaluatorRejectsNullAndNonStringFieldsInResponse(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Case 1: index is explicitly null
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-null","total":1,"items":[{"index":null,"source_path":"doc.md","status":"succeeded"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server1.Close()

	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)
	if _, err := eval1.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on index: null, got nil")
	}

	// Case 2: source_path is a number/non-string
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-num","total":1,"items":[{"source_path":12345,"status":"succeeded"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)
	if _, err := eval2.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on non-string source_path, got nil")
	}
}

func TestEvaluatorRejectsConflictingSourcePathEvenIfFilenameMatches(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// wrong.md source_path with matching doc.md filename -> MUST FAIL CLOSED
			return `{"status":"ok","batch_id":"batch-wrong-sp","total":1,"items":[{"source_path":"wrong.md","filename":"doc.md","status":"succeeded"}],"errors":[]}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1,"entity_types":{"Service":1}}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on wrong source_path, got nil")
	}
	if !strings.Contains(err.Error(), "unrecognized sample") {
		t.Fatalf("expected unrecognized sample error, got: %v", err)
	}
}

func TestEvaluatorRejectsNonListAndNonObjectItems(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// items is a string, not a list
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-not-list","items":"not-a-list"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server1.Close()

	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)
	if _, err := eval1.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on items: string, got nil")
	}

	// items has a string element, not an object
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-not-obj","items":["not-an-object"]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)
	if _, err := eval2.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on items entry not object, got nil")
	}
}

func TestEvaluatorRejectsSuccessEnvelopeWithoutDetailList(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-no-items"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on missing detail list, got nil")
	}
	if !strings.Contains(err.Error(), "missing items or jobs detail list") {
		t.Fatalf("expected missing detail list error, got: %v", err)
	}
}

func TestEvaluatorRejectsPendingEnvelopeWithoutJobID(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Pending status with no job_id at all
			return `{"status":"pending","batch_id":"batch-no-job"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	if _, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on pending envelope without job_id, got nil")
	}
}

func TestEvaluatorRejectsDuplicateBasenameLooseAcknowledgement(t *testing.T) {
	tempDir := t.TempDir()
	dirA := filepath.Join(tempDir, "a")
	dirB := filepath.Join(tempDir, "b")
	_ = os.MkdirAll(dirA, 0755)
	_ = os.MkdirAll(dirB, 0755)
	_ = os.WriteFile(filepath.Join(dirA, "doc.md"), []byte("# Doc A"), 0644)
	_ = os.WriteFile(filepath.Join(dirB, "doc.md"), []byte("# Doc B"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Acknowledges single "doc.md" without index or distinct path
			return `{"status":"ok","batch_id":"batch-dup","total":2,"items":[{"filename":"doc.md","status":"succeeded"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	if _, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 2, ThresholdOther: 10}); err == nil {
		t.Fatalf("expected fail-closed on duplicate basename loose acknowledgement, got nil")
	}
}

func TestEvaluatorRejectsOverflowEntityCounts(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Graph status reports an entity_count exceeding MaxSafeEntityCount
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":1e16}}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail on overflow entity count > 1<<53, got nil")
	}
}

func TestEvaluatorParsesTopEntitiesMentions(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":2,"entity_types":{"Service":1,"Other":1}},"top_entities":[{"entity":"UnkConcept","type":"Other","mentions":5}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	res, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 60,
	})
	if err != nil {
		t.Fatalf("TestOntology failed: %v", err)
	}
	if len(res.UnclassifiedConcepts) != 1 {
		t.Fatalf("expected 1 unclassified concept, got %d", len(res.UnclassifiedConcepts))
	}
	if res.UnclassifiedConcepts[0].Name != "UnkConcept" || res.UnclassifiedConcepts[0].Frequency != 5 {
		t.Errorf("expected UnkConcept with frequency 5, got %+v", res.UnclassifiedConcepts[0])
	}
}

func TestEvaluatorRejectsMalformedMentionsInTopEntities(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// top_entities contains negative mentions
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-1","total":1,"items":[{"index":0,"source_path":"doc.md","job_id":"job-1","status":"queued"}],"errors":[]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"document_count":1,"entity_count":2,"entity_types":{"Service":1,"Other":1}},"top_entities":[{"entity":"UnkConcept","type":"Other","mentions":-5}]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 60,
	})
	if err == nil {
		t.Fatalf("expected TestOntology to fail on negative mentions in top_entities, got nil")
	}
}

func TestEvaluatorCancelsDiscoveredJobsWhenResponseHasSchemaError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	cancelledJobs := make(map[string]bool)
	var deleteCalled bool
	var allCancelledBeforeDelete = true

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Top-level job_id, valid job in jobs[], and valid job in items[] alongside a malformed sibling
			return `{"status":"ok","batch_id":"batch-err","job_id":"job-top-1","items":[{"job_id":"job-item-2"},"malformed-item"],"jobs":[{"job_id":"job-jobs-3"}]}`, false
		case "long_ingest_cancel":
			if jobID, ok := args["job_id"].(string); ok {
				cancelledJobs[jobID] = true
			}
			if deleteCalled {
				allCancelledBeforeDelete = false
			}
			return `{"status":"ok"}`, false
		case "space_delete":
			deleteCalled = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on schema error, got nil")
	}
	expectedJobs := []string{"job-top-1", "job-item-2", "job-jobs-3"}
	for _, jID := range expectedJobs {
		if !cancelledJobs[jID] {
			t.Fatalf("expected job %s to be cancelled, but was not", jID)
		}
	}
	if !allCancelledBeforeDelete {
		t.Fatalf("expected all jobs to be cancelled BEFORE space_delete")
	}
}

func TestEvaluatorCancelsDiscoveredJobsWhenResponseHasIsError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	cancelledJobs := make(map[string]bool)
	var deleteCalled bool
	var allCancelledBeforeDelete = true

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// isError envelope with discovered jobs
			return `{"status":"error","isError":true,"message":"partial failure after job launch","job_id":"job-err-top","jobs":[{"job_id":"job-err-1"}]}`, false
		case "long_ingest_cancel":
			if jobID, ok := args["job_id"].(string); ok {
				cancelledJobs[jobID] = true
			}
			if deleteCalled {
				allCancelledBeforeDelete = false
			}
			return `{"status":"ok"}`, false
		case "space_delete":
			deleteCalled = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err == nil {
		t.Fatalf("expected fail-closed on isError response, got nil")
	}
	expectedJobs := []string{"job-err-top", "job-err-1"}
	for _, jID := range expectedJobs {
		if !cancelledJobs[jID] {
			t.Fatalf("expected job %s to be cancelled on isError, but was not", jID)
		}
	}
	if !allCancelledBeforeDelete {
		t.Fatalf("expected all jobs to be cancelled BEFORE space_delete")
	}
}

func TestEvaluatorRejectsBlankSourcePathAndFilename(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// Blank source_path
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-blank-sp","total":1,"items":[{"source_path":"   ","filename":"doc.md","status":"succeeded"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server1.Close()

	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)
	_, err1 := eval1.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err1 == nil {
		t.Fatalf("expected fail-closed on blank source_path, got nil")
	}
	if !strings.Contains(err1.Error(), "source_path field cannot be blank") {
		t.Fatalf("expected 'source_path field cannot be blank' error, got: %v", err1)
	}

	// Blank filename
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-blank-fn","total":1,"items":[{"source_path":"doc.md","filename":"","status":"succeeded"}],"errors":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)
	_, err2 := eval2.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err2 == nil {
		t.Fatalf("expected fail-closed on blank filename, got nil")
	}
	if !strings.Contains(err2.Error(), "filename field cannot be blank") {
		t.Fatalf("expected 'filename field cannot be blank' error, got: %v", err2)
	}
}

func TestEvaluatorRejectsPresentNullListFields(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	// items is explicitly null with valid jobs
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-null-items","items":null,"jobs":[]}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server1.Close()

	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)
	_, err1 := eval1.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err1 == nil {
		t.Fatalf("expected fail-closed on items: null, got nil")
	}
	if !strings.Contains(err1.Error(), "'items' field cannot be null") {
		t.Fatalf("expected ''items' field cannot be null' error, got: %v", err1)
	}

	// jobs is explicitly null with valid items
	server2 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"batch-null-jobs","items":[],"jobs":null}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server2.Close()

	client2 := mcpclient.NewClient(server2.URL, "tok", 10*time.Second)
	eval2 := NewEvaluator(client2)
	_, err2 := eval2.TestOntology(context.Background(), tempDir, EvalOptions{Ontology: "entities:\n  - name: Service", SampleSize: 1, ThresholdOther: 10})
	if err2 == nil {
		t.Fatalf("expected fail-closed on jobs: null, got nil")
	}
	if !strings.Contains(err2.Error(), "'jobs' field cannot be null") {
		t.Fatalf("expected ''jobs' field cannot be null' error, got: %v", err2)
	}
}

func TestEvaluatorRejectsAlreadyExistingSpaceWithoutAuthorization(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"already_exists"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"ok"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	// Case 1: TargetSpace specified without AllowExistingSpace -> fails closed
	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SpaceID:        "existing-space",
		SampleSize:     1,
		ThresholdOther: 10,
	})
	if err == nil {
		t.Fatalf("expected fail-closed on already_exists space without authorization, got nil")
	}
	if !strings.Contains(err.Error(), "refusing destructive evaluation against existing space without explicit authorization") {
		t.Fatalf("unexpected error message: %v", err)
	}
}

func TestEvaluatorCleanupDeletesSpaceOnlyWhenJobsAreTerminal(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-term-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			return `{"status":"cancelled"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"injected failure to trigger cleanup"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, _ = eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if !spaceDeleted {
		t.Fatalf("expected space_delete to be called when job reaches confirmed terminal state")
	}
}

func TestEvaluatorCleanupRetainsSpaceWhenJobFailsTerminality(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-nonterm-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"error","message":"cancellation failed"}`, false
		case "long_ingest_status":
			return `{"status":"running"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"injected failure to trigger cleanup"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, _ = eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when job fails to reach terminal state (space should be retained)")
	}
}

func TestEvaluatorCleanupRetainsSpaceOnMCPIsError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-err-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			// MCP error envelope: isError=true must NOT be treated as a positive terminal job state
			return `{"status":"error","message":"status inspection failed"}`, true
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"injected failure to trigger cleanup"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, _ = eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when status inspection returns MCP isError:true (space should be retained)")
	}
}

func TestEvaluatorCleanupRetainsSpaceOnPlainStatusError(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-err-2"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			// Plain status:error response must NOT be accepted as a positive terminal job state
			return `{"status":"error","message":"job lookup internal error"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"injected failure to trigger cleanup"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, _ = eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when status returns status:error (space should be retained)")
	}
}

func TestEvaluatorCleanupRetainsSpaceOnUnknownJobStatus(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-unk-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			return `{"status":"non_terminal_custom_state"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"injected failure to trigger cleanup"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, _ = eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when status returns unknown status (space should be retained)")
	}
}

func TestEvaluatorCleanupRetainsSpaceOnAmbiguousSubmissionFailure(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// Submission attempt fails/times out -> ambiguous outcome, space must be retained
			return `{"status":"error","message":"transport lost after queuing"}`, true
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if err == nil {
		t.Fatalf("expected error on failed submission, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called on ambiguous submission failure")
	}
}

func TestEvaluatorPollJobRejectsNonJobStatuses(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	for _, invalidStatus := range []string{"completed", "queue_full", "rejected", "ok", "indexed", "success", "custom_active"} {
		spaceDeleted := false
		statusCalls := 0
		server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
			switch name {
			case "ontology_validate":
				return `{"status":"ok","valid":true}`, false
			case "space_create":
				return `{"status":"created"}`, false
			case "long_ingest_async":
				return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-nonjob-1"}]}`, false
			case "long_ingest_cancel":
				return `{"status":"cancelling"}`, false
			case "long_ingest_status":
				statusCalls++
				return fmt.Sprintf(`{"status":"%s"}`, invalidStatus), false
			case "space_delete":
				spaceDeleted = true
				return `{"status":"ok"}`, false
			default:
				return `{"status":"error","message":"unexpected tool"}`, false
			}
		})

		client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
		eval := NewEvaluator(client)
		eval.PollInterval = 10 * time.Millisecond

		_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
			Ontology:       "entities:\n  - name: Service",
			SampleSize:     1,
			ThresholdOther: 10,
		})

		server.Close()

		if statusCalls == 0 {
			t.Fatalf("expected status endpoint to be called, got 0 calls")
		}
		if err == nil {
			t.Fatalf("expected error when pollJob receives non-job status %q, got nil", invalidStatus)
		}
		if spaceDeleted {
			t.Fatalf("expected space_delete to NOT be called when status is non-job %q", invalidStatus)
		}
	}
}

func TestEvaluatorCustomTimeoutAppliedToPolling(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	statusCalls := 0
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-timeout-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			statusCalls++
			// Keep pending to trigger timeout
			return `{"status":"running"}`, false
		case "space_delete":
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)
	eval.PollInterval = 10 * time.Millisecond

	start := time.Now()
	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
		Timeout:        200 * time.Millisecond,
	})
	elapsed := time.Since(start)

	if statusCalls == 0 {
		t.Fatalf("expected status endpoint to be polled before timeout, got 0 calls")
	}
	if err == nil {
		t.Fatalf("expected timeout error, got nil")
	}
	if elapsed > 2*time.Second {
		t.Fatalf("expected polling to respect custom timeout of 200ms, elapsed %v", elapsed)
	}
}

func TestEvaluatorPollJobRejectsIsErrorWithStateCompleted(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-err-state-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			// isError=true with state="completed" must be rejected fail-closed
			return `{"status":"error","state":"completed","message":"inspection error"}`, true
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if err == nil {
		t.Fatalf("expected error on isError:true with state:completed, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called on isError:true with state:completed")
	}
}

func TestEvaluatorPollJobRejectsStatusErrorWithStateCompleted(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-err-state-2"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			// status="error" with state="completed" must be rejected fail-closed
			return `{"status":"error","state":"completed","message":"plain status error"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
	})

	if err == nil {
		t.Fatalf("expected error on status:error with state:completed, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called on status:error with state:completed")
	}
}

func TestEvaluatorRejectsConflictingStatusStateOverride(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc"), 0644)

	spaceDeleted := false
	statusCalls := 0
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"pending","job_id":"job-conflict-1"}]}`, false
		case "long_ingest_cancel":
			return `{"status":"cancelling"}`, false
		case "long_ingest_status":
			statusCalls++
			// status is "running" (non-terminal), state is "succeeded" -> must NOT treat as terminal success
			return `{"status":"running","state":"succeeded"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)
	eval.PollInterval = 10 * time.Millisecond

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 10,
		Timeout:        200 * time.Millisecond,
	})

	if statusCalls == 0 {
		t.Fatalf("expected status endpoint to be called, got 0 calls")
	}
	if err == nil {
		t.Fatalf("expected error when status:running (conflicting state:succeeded), got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when status is running (despite state:succeeded)")
	}
}

func TestEvaluatorPollJobDirectRejectionOfNonJobStatuses(t *testing.T) {
	for _, invalidStatus := range []string{"completed", "queue_full", "rejected", "ok", "indexed", "success", "custom_active"} {
		statusCalls := 0
		server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
			if name == "long_ingest_status" {
				statusCalls++
				return fmt.Sprintf(`{"status":"%s"}`, invalidStatus), false
			}
			return `{"status":"error","message":"unexpected tool"}`, false
		})

		client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
		eval := NewEvaluator(client)
		eval.PollInterval = 10 * time.Millisecond

		err := eval.pollJob(context.Background(), "sp1", "job-nonjob-1", 200*time.Millisecond)
		server.Close()

		if statusCalls != 1 {
			t.Fatalf("expected direct status endpoint to be called exactly once and immediately rejected, got %d calls", statusCalls)
		}
		if err == nil {
			t.Fatalf("expected pollJob to fail for non-job status %q, got nil", invalidStatus)
		}
		if !strings.Contains(err.Error(), "received non-job/unrecognized status") {
			t.Fatalf("expected error message to contain 'received non-job/unrecognized status', got %q", err.Error())
		}
	}
}

func TestEvaluatorPollJobDirectRejectionOfConflictingStatusState(t *testing.T) {
	statusCalls := 0
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		if name == "long_ingest_status" {
			statusCalls++
			// status is "running" (non-terminal), state is "succeeded" -> must NOT return nil (success)
			return `{"status":"running","state":"succeeded"}`, false
		}
		return `{"status":"error","message":"unexpected tool"}`, false
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)
	eval.PollInterval = 10 * time.Millisecond

	err := eval.pollJob(context.Background(), "sp1", "job-conflict-1", 100*time.Millisecond)
	if statusCalls == 0 {
		t.Fatalf("expected direct status endpoint to be called, got 0 calls")
	}
	if err == nil {
		t.Fatalf("expected pollJob to timeout/fail on running status (despite state:succeeded), got nil")
	}
}

func TestEvaluatorSpaceDeleteStrictCanonicalDeleted(t *testing.T) {
	// Negative statuses that must be rejected as cleanup failures
	nonCanonicalStatuses := []string{"ok", "success", "succeeded", "partial", "", "any_other"}
	for _, nonCanonical := range nonCanonicalStatuses {
		tempDir := t.TempDir()
		docPath := filepath.Join(tempDir, "doc.md")
		_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

		server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
			switch name {
			case "ontology_validate":
				return `{"status":"ok","valid":true}`, false
			case "space_create":
				return `{"status":"created"}`, false
			case "long_ingest_async":
				return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"succeeded","job_id":"job-1"}]}`, false
			case "long_ingest_status":
				return `{"status":"succeeded"}`, false
			case "long_status":
				return `{"status":"ok","graph_stats":{"entity_count":10,"entity_types":{"Service":8,"Other":2}}}`, false
			case "space_delete":
				return fmt.Sprintf(`{"status":%q}`, nonCanonical), false
			default:
				return `{"status":"error","message":"unexpected tool"}`, false
			}
		})

		client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
		eval := NewEvaluator(client)

		res, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
			Ontology:       "entities:\n  - name: Service",
			SampleSize:     1,
			ThresholdOther: 50,
		})

		server.Close()

		if err != nil {
			t.Fatalf("expected evaluation itself to succeed, got error: %v", err)
		}
		if res.SpaceCleanupStatus != "failed" {
			t.Fatalf("expected SpaceCleanupStatus='failed' for non-canonical status %q, got %q", nonCanonical, res.SpaceCleanupStatus)
		}
	}

	// Canonical "deleted" status must succeed
	{
		tempDir := t.TempDir()
		docPath := filepath.Join(tempDir, "doc.md")
		_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

		server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
			switch name {
			case "ontology_validate":
				return `{"status":"ok","valid":true}`, false
			case "space_create":
				return `{"status":"created"}`, false
			case "long_ingest_async":
				return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"succeeded","job_id":"job-1"}]}`, false
			case "long_ingest_status":
				return `{"status":"succeeded"}`, false
			case "long_status":
				return `{"status":"ok","graph_stats":{"entity_count":10,"entity_types":{"Service":8,"Other":2}}}`, false
			case "space_delete":
				return `{"status":"deleted","space_id":"sp-del-1"}`, false
			default:
				return `{"status":"error","message":"unexpected tool"}`, false
			}
		})
		defer server.Close()

		client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
		eval := NewEvaluator(client)

		res, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
			Ontology:       "entities:\n  - name: Service",
			SampleSize:     1,
			ThresholdOther: 50,
		})

		if err != nil {
			t.Fatalf("expected evaluation to succeed, got error: %v", err)
		}
		if res.SpaceCleanupStatus != "cleaned" {
			t.Fatalf("expected SpaceCleanupStatus='cleaned' for canonical status 'deleted', got %q", res.SpaceCleanupStatus)
		}
	}
}

func TestEvaluatorSpaceDeleteTransportFailureTrackedInResult(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

	// Case 1: isError=true (tool error envelope)
	server1 := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"succeeded","job_id":"job-1"}]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"ok","graph_stats":{"entity_count":10,"entity_types":{"Service":8,"Other":2}}}`, false
		case "space_delete":
			return `{"status":"error","message":"space_delete storage backend failure"}`, true
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server1.Close()

	client1 := mcpclient.NewClient(server1.URL, "tok", 10*time.Second)
	eval1 := NewEvaluator(client1)

	res1, err1 := eval1.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 50,
	})

	if err1 != nil {
		t.Fatalf("expected evaluation to succeed, got error: %v", err1)
	}
	if res1.SpaceCleanupStatus != "failed" {
		t.Fatalf("expected SpaceCleanupStatus='failed' on isError=true, got %q", res1.SpaceCleanupStatus)
	}
}

func TestEvaluatorAmbiguousSubmissionRetainsSpaceWithActionableDiagnostic(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

	spaceDeleted := false
	createdSpaceID := ""
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			createdSpaceID, _ = args["space_id"].(string)
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"error","message":"ambiguous connection lost during batch submission"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 50,
	})

	if err == nil {
		t.Fatalf("expected evaluation to fail on submission error, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called on ambiguous submission failure")
	}
	if createdSpaceID == "" || !strings.Contains(err.Error(), createdSpaceID) {
		t.Fatalf("expected error message to contain actionable temporary space ID %q, got %q", createdSpaceID, err.Error())
	}
}

func TestEvaluatorSchemaErrorRetainsSpaceWithActionableDiagnostic(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

	spaceDeleted := false
	createdSpaceID := ""
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			createdSpaceID, _ = args["space_id"].(string)
			return `{"status":"created"}`, false
		case "long_ingest_async":
			// items: null is a schema violation
			return `{"status":"ok","items":null}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 50,
	})

	if err == nil {
		t.Fatalf("expected evaluation to fail on items:null schema error, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called on schema error")
	}
	if createdSpaceID == "" || !strings.Contains(err.Error(), createdSpaceID) {
		t.Fatalf("expected error message to contain actionable temporary space ID %q, got %q", createdSpaceID, err.Error())
	}
}

func TestEvaluatorKeepMemoryFailureRetainsSpaceWithActionableDiagnostic(t *testing.T) {
	tempDir := t.TempDir()
	docPath := filepath.Join(tempDir, "doc.md")
	_ = os.WriteFile(docPath, []byte("# Test Doc\nSome text"), 0644)

	spaceDeleted := false
	createdSpaceID := ""
	server := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "space_create":
			createdSpaceID, _ = args["space_id"].(string)
			return `{"status":"created"}`, false
		case "long_ingest_async":
			return `{"status":"ok","batch_id":"b-1","items":[{"source_path":"doc.md","status":"succeeded","job_id":"job-1"}]}`, false
		case "long_ingest_status":
			return `{"status":"succeeded"}`, false
		case "long_status":
			return `{"status":"error","message":"graph index failed"}`, false
		case "space_delete":
			spaceDeleted = true
			return `{"status":"ok"}`, false
		default:
			return `{"status":"error","message":"unexpected tool"}`, false
		}
	})
	defer server.Close()

	client := mcpclient.NewClient(server.URL, "tok", 10*time.Second)
	eval := NewEvaluator(client)

	_, err := eval.TestOntology(context.Background(), tempDir, EvalOptions{
		Ontology:       "entities:\n  - name: Service",
		SampleSize:     1,
		ThresholdOther: 50,
		KeepMemory:     true,
	})

	if err == nil {
		t.Fatalf("expected evaluation to fail on graph index error, got nil")
	}
	if spaceDeleted {
		t.Fatalf("expected space_delete to NOT be called when KeepMemory=true")
	}
	if createdSpaceID == "" || !strings.Contains(err.Error(), createdSpaceID) {
		t.Fatalf("expected error message to contain actionable temporary space ID %q, got %q", createdSpaceID, err.Error())
	}
	if !strings.Contains(err.Error(), "--keep-memory") {
		t.Fatalf("expected error message to mention --keep-memory, got %q", err.Error())
	}
}

func mustJSON(s string) []byte {
	b, _ := json.Marshal(s)
	return b
}
