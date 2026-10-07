package ingest

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/scanner"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

func bulkFiles(t *testing.T, count int) string {
	t.Helper()
	dir := t.TempDir()
	for i := 0; i < count; i++ {
		group := "a"
		if i >= count-2 {
			group = fmt.Sprintf("z%d", i)
		}
		path := filepath.Join(dir, group, fmt.Sprintf("%03d.md", i))
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(fmt.Sprintf("# Complete source %d\nBeginning.\nFinal statement %d.\n", i, i)), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestBatchSizeClampReportsEffectiveLimit(t *testing.T) {
	var messages []string
	_, err := NewEngine(nil).Run(context.Background(), IngestOptions{
		Path: t.TempDir(), DryRun: true, BatchSizeMB: 100,
	}, func(stage, message string, current, total int, file, jobID, status, errStr string) {
		if stage == "configuration" {
			messages = append(messages, message)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(messages) != 1 || !strings.Contains(messages[0], "100") || !strings.Contains(messages[0], "50 MiB") {
		t.Fatalf("configured limit override was not reported: %v", messages)
	}
}

func TestAutomaticBulkBootstrapAndUnchangedResume(t *testing.T) {
	dir := bulkFiles(t, 24)
	var mu sync.Mutex
	known := map[string]string{}
	pending := map[string]string{}
	var bootstraps [][]string
	submitted := 0
	round := 0
	srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		mu.Lock()
		defer mu.Unlock()
		switch name {
		case "space_info":
			return `{"status":"ok"}`, false
		case "long_ingest_list":
			return `{"status":"ok","jobs":[]}`, false
		case "long_document_list":
			docs := []map[string]interface{}{}
			for path, hash := range known {
				docs = append(docs, map[string]interface{}{"source_path": path, "sha256": hash, "ingestion_status": "succeeded"})
			}
			return string(bulkJSON(map[string]interface{}{"status": "ok", "documents": docs})), false
		case "ontology_get", "ontology_validate":
			t.Errorf("automatic mode must not resolve an ontology library entry")
			return `{"status":"error","message":"auto is not a library entry"}`, false
		case "long_ingest_async":
			submitted++
			if len(pending) > 0 {
				t.Errorf("submitted a batch before prior jobs finished")
			}
			docs := args["documents"].([]interface{})
			options := args["options"].(map[string]interface{})
			auto := options["ontology"] == "auto"
			if auto {
				paths := []string{}
				for _, raw := range docs {
					paths = append(paths, raw.(map[string]interface{})["source_path"].(string))
				}
				sort.Strings(paths)
				bootstraps = append(bootstraps, paths)
				if len(paths) != 12 {
					t.Errorf("want12 bootstrap sources, got%d", len(paths))
				}
				for _, suffix := range []string{"z22/022.md", "z23/023.md"} {
					if !strings.Contains(strings.Join(paths, "\n"), suffix) {
						t.Errorf("bootstrap missed distant stratum%s: %v", suffix, paths)
					}
				}
			} else {
				if _, ok := options["ontology"]; ok {
					t.Error("remaining batch overrides frozen default")
				}
				if round == 0 {
					return `{"status":"error","message":"simulated interruption after bootstrap"}`, false
				}
			}
			items := []map[string]interface{}{}
			for _, raw := range docs {
				doc := raw.(map[string]interface{})
				path := doc["source_path"].(string)
				data, err := base64.StdEncoding.DecodeString(doc["content_base64"].(string))
				if err != nil {
					t.Error(err)
				}
				original, _ := os.ReadFile(filepath.Join(dir, path))
				if !reflect.DeepEqual(data, original) {
					t.Errorf("partial source submitted:%s", path)
				}
				hash := sha256.Sum256(data)
				if doc["sha256"] != hex.EncodeToString(hash[:]) {
					t.Error("checksum mismatch")
				}
				status := "queued"
				jobID := fmt.Sprintf("%d-%s", submitted, path)
				if known[path] == doc["sha256"] {
					status = "skipped"
				} else {
					pending[jobID] = path
				}
				items = append(items, map[string]interface{}{"source_path": path, "status": status, "job_id": jobID})
			}
			return string(bulkJSON(map[string]interface{}{"status": "ok", "items": items})), false
		case "long_ingest_status":
			id := args["job_id"].(string)
			path, ok := pending[id]
			if !ok {
				t.Errorf("unknown pending job%s", id)
			}
			data, _ := os.ReadFile(filepath.Join(dir, path))
			hash := sha256.Sum256(data)
			known[path] = hex.EncodeToString(hash[:])
			delete(pending, id)
			return `{"status":"succeeded"}`, false
		default:
			t.Errorf("unexpected tool%s", name)
			return `{"status":"error"}`, false
		}
	})
	defer srv.Close()
	opts := IngestOptions{Path: dir, SpaceID: "bulk", Ontology: "auto", WatchJobs: true, BatchSizeMB: 1, Timeout: time.Second}
	run := func() *IngestResult {
		e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
		e.PollInterval = time.Millisecond
		res, err := e.Run(context.Background(), opts, nil)
		if err != nil {
			t.Fatalf("run failed:%v", err)
		}
		return res
	}
	first := run()
	if first.Success || first.TotalSucceeded != 12 || first.TotalFailed != 12 {
		t.Fatalf("unexpected interrupted result:%+v", first)
	}
	mu.Lock()
	round = 1
	mu.Unlock()
	second := run()
	if !second.Success || second.TotalSkipped != 12 || second.TotalSucceeded != 12 {
		t.Fatalf("resume:%+v", second)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(known) != 24 || len(bootstraps) != 2 || !reflect.DeepEqual(bootstraps[0], bootstraps[1]) {
		t.Fatalf("changed bootstrap/missing docs:%v known%d", bootstraps, len(known))
	}
}

func TestAutomaticNoPollRejectedWithoutServerEffects(t *testing.T) {
	calls := 0
	srv := newMockServer(func(string, map[string]interface{}) (string, bool) {
		calls++
		return `{"status":"ok","documents":[]}`, false
	})
	defer srv.Close()
	e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
	_, err := e.Run(context.Background(), IngestOptions{Path: bulkFiles(t, 1), SpaceID: "bulk", Ontology: "auto", WatchJobs: false}, nil)
	if err == nil || !strings.Contains(err.Error(), "requires --watch") || calls != 0 {
		t.Fatalf("want upfront watch validation, err=%v calls%d", err, calls)
	}
}

func TestAutomaticBootstrapFailureStopsBulk(t *testing.T) {
	calls := 0
	srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok"}`, false
		case "long_ingest_list", "long_document_list":
			return `{"status":"ok","documents":[]}`, false
		case "ontology_get":
			return `{"status":"ok","content_yaml":"legacy"}`, false
		case "ontology_validate":
			return `{"status":"ok","valid":true}`, false
		case "long_ingest_async":
			calls++
			if args["options"].(map[string]interface{})["ontology"] != "auto" {
				t.Error("fallback ontology used")
			}
			return `{"status":"error","message":"construction refused"}`, false
		default:
			return `{"status":"error"}`, false
		}
	})
	defer srv.Close()
	e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
	res, err := e.Run(context.Background(), IngestOptions{Path: bulkFiles(t, 24), SpaceID: "bulk", Ontology: "auto", WatchJobs: true}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if res.Success || calls != 1 || len(res.Jobs) != 24 {
		t.Fatalf("bootstrap failure not accounted for: calls%d result%+v", calls, res)
	}
	for _, j := range res.Jobs {
		if j.Status != "failed" && j.Status != "not_submitted" {
			t.Errorf("unexpected final status%+v", j)
		}
	}
}

func TestWatchedBulkBoundsAdmissionAndRetriesOnlyRefused(t *testing.T) {
	var mu sync.Mutex
	pending := map[string]bool{}
	submissions := map[string]int{}
	batches := 0
	rejected := "a/000.md"
	srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		mu.Lock()
		defer mu.Unlock()
		switch name {
		case "space_info":
			return `{"status":"ok"}`, false
		case "long_ingest_list", "long_document_list":
			return `{"status":"ok","documents":[]}`, false
		case "long_ingest_async":
			batches++
			if len(pending) > 0 {
				t.Errorf("new batch while%d prior jobs pending", len(pending))
			}
			docs := args["documents"].([]interface{})
			if len(docs) > 200 {
				t.Errorf("batch count exceeds queue:%d", len(docs))
			}
			items := []map[string]interface{}{}
			for _, raw := range docs {
				path := raw.(map[string]interface{})["source_path"].(string)
				submissions[path]++
				status := "queued"
				id := path
				if path == rejected && submissions[path] == 1 {
					status = "queue_full"
					id = ""
				} else {
					pending[id] = true
				}
				items = append(items, map[string]interface{}{"source_path": path, "status": status, "job_id": id})
			}
			return string(bulkJSON(map[string]interface{}{"status": "ok", "items": items})), false
		case "long_ingest_status":
			delete(pending, args["job_id"].(string))
			return `{"status":"succeeded"}`, false
		default:
			return `{"status":"error"}`, false
		}
	})
	defer srv.Close()
	e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
	e.PollInterval = time.Millisecond
	res, err := e.Run(context.Background(), IngestOptions{Path: bulkFiles(t, 205), SpaceID: "bulk", WatchJobs: true, Timeout: time.Second}, nil)
	if err != nil || !res.Success || res.TotalSucceeded != 205 || res.TotalFailed != 0 {
		t.Fatalf("bulk failed:%+v err%v", res, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if batches < 3 {
		t.Errorf("expected bounded batches and retry, got%d", batches)
	}
	for path, count := range submissions {
		want := 1
		if path == rejected {
			want = 2
		}
		if count != want {
			t.Errorf("%s sent%d times, want%d", path, count, want)
		}
	}
}

func TestAutomaticRefusesChangedSelectedBytes(t *testing.T) {
	dir := bulkFiles(t, 1)
	scan, err := scanner.Scan(scanner.ScanOptions{RootPath: dir})
	if err != nil {
		t.Fatal(err)
	}
	file := scan.Batches[0].Files[0]
	if err := os.WriteFile(file.Path, []byte("Changed after inventory"), 0600); err != nil {
		t.Fatal(err)
	}
	calls := 0
	srv := newMockServer(func(string, map[string]interface{}) (string, bool) { calls++; return `{"status":"error"}`, false })
	defer srv.Close()
	e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
	_, err = e.IngestBatch(context.Background(), "bulk", scan.Batches[0], "auto", 0, false)
	if err == nil || !strings.Contains(err.Error(), "changed since scan") || calls != 0 {
		t.Fatalf("stale bootstrap submitted: err%v calls%d", err, calls)
	}
}

func bulkJSON(v interface{}) []byte { data, _ := json.Marshal(v); return data }

func TestBulkAdmissionRetriesAreBoundedAndExplicit(t *testing.T) {
	for _, tc := range []struct {
		name     string
		watch    bool
		response string
		want     int
	}{
		{"capacity", true, "queue_full", 4}, {"no_poll", false, "queue_full", 1}, {"authentication", true, "error", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			polls := 0
			srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
				switch name {
				case "space_info":
					return `{"status":"ok"}`, false
				case "long_ingest_list", "long_document_list":
					return `{"status":"ok","documents":[]}`, false
				case "long_ingest_async":
					calls++
					if tc.response == "error" {
						return `{"status":"error","message":"access denied"}`, false
					}
					doc := args["documents"].([]interface{})[0].(map[string]interface{})
					return string(bulkJSON(map[string]interface{}{"status": "ok", "items": []map[string]interface{}{{"source_path": doc["source_path"], "status": "queue_full"}}})), false
				case "long_ingest_status":
					polls++
					return `{"status":"failed"}`, false
				default:
					return `{"status":"error"}`, false
				}
			})
			defer srv.Close()
			e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
			e.PollInterval = time.Millisecond
			res, err := e.Run(context.Background(), IngestOptions{Path: bulkFiles(t, 1), SpaceID: "bulk", WatchJobs: tc.watch}, nil)
			if err != nil || res.Success || res.TotalFailed != 1 || calls != tc.want || polls != 0 {
				t.Fatalf("calls%d polls%d res%+v err%v", calls, polls, res, err)
			}
		})
	}
}

func TestAutomaticFailedJobStopsRemainder(t *testing.T) {
	submits := 0
	polls := 0
	srv := newMockServer(func(name string, args map[string]interface{}) (string, bool) {
		switch name {
		case "space_info":
			return `{"status":"ok"}`, false
		case "long_ingest_list", "long_document_list":
			return `{"status":"ok","documents":[]}`, false
		case "long_ingest_async":
			submits++
			items := []map[string]interface{}{}
			for _, raw := range args["documents"].([]interface{}) {
				d := raw.(map[string]interface{})
				items = append(items, map[string]interface{}{"source_path": d["source_path"], "status": "queued", "job_id": d["source_path"]})
			}
			return string(bulkJSON(map[string]interface{}{"status": "ok", "items": items})), false
		case "long_ingest_status":
			polls++
			return `{"status":"failed","error":"construction interrupted"}`, false
		default:
			return `{"status":"error"}`, false
		}
	})
	defer srv.Close()
	e := NewEngine(mcpclient.NewClient(srv.URL, "token", time.Second))
	e.PollInterval = time.Millisecond
	res, err := e.Run(context.Background(), IngestOptions{Path: bulkFiles(t, 24), SpaceID: "bulk", Ontology: "auto", WatchJobs: true}, nil)
	if err != nil || res.Success || submits != 1 || polls != 12 || res.TotalNotSubmitted != 12 || res.TotalFailed != 12 {
		t.Fatalf("submits%d polls%d res%+v err%v", submits, polls, res, err)
	}
}

func TestAutomaticSelectionBudgetAndDryRun(t *testing.T) {
	dir := bulkFiles(t, 24)
	e := NewEngine(mcpclient.NewClient("http://127.0.0.1:1", "", time.Second))
	res, err := e.Run(context.Background(), IngestOptions{Path: dir, Ontology: "auto", DryRun: true}, nil)
	if err != nil || !res.Success || len(res.AutomaticSources) != 12 || res.TotalScanned != 24 {
		t.Fatalf("offline plan:%+v err%v", res, err)
	}
	// Reorder the inventory; selection is independent of traversal and bounded in bytes.
	files := []scanner.FileItem{{RelPath: "a/a.md", SHA256: "a", Size: 60}, {RelPath: "b/b.txt", SHA256: "b", Size: 60}, {RelPath: "empty.md", SHA256: "e", Size: 0}}
	var previous string
	for i := 0; i < 2; i++ {
		scan := &scanner.ScanResult{TotalFiles: 3, Batches: []scanner.Batch{{Files: append([]scanner.FileItem(nil), files...)}}}
		selected, err := planAutomatic(scan, nil, 100, false)
		if err != nil || len(selected) != 1 || scan.Batches[0].TotalSize != 60 {
			t.Fatalf("unbounded selection:%v err%v", selected, err)
		}
		if i > 0 && selected[0].RelPath != previous {
			t.Fatal("selection depends on inventory order")
		}
		previous = selected[0].RelPath
		files[0], files[1] = files[1], files[0]
	}
}

func TestAutomaticSelectionCoversRootGroupsBeforeDeepFolders(t *testing.T) {
	var files []scanner.FileItem
	for group := 0; group < 8; group++ {
		count := 1
		if group == 0 {
			count = 60
		}
		for i := 0; i < count; i++ {
			path := fmt.Sprintf("group%d/folder%02d/doc.md", group, i)
			files = append(files, scanner.FileItem{RelPath: path, SHA256: path, Size: 100})
		}
	}
	scan := &scanner.ScanResult{TotalFiles: len(files), Batches: []scanner.Batch{{Files: files}}}
	selected, err := planAutomatic(scan, nil, 1200, false)
	if err != nil {
		t.Fatal(err)
	}
	groups := map[string]bool{}
	for _, file := range selected[:8] {
		groups[strings.Split(file.RelPath, "/")[0]] = true
	}
	if len(groups) != 8 {
		t.Fatalf("large deep group crowded out root groups:%v", selected)
	}
}
