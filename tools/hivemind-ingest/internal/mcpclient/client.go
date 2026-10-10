package mcpclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	maxResponseBytes = 16 * 1024 * 1024 // 16 MB maximum HTTP response
)

// JSONRPCRequest represents a JSON-RPC 2.0 request to an MCP server
type JSONRPCRequest struct {
	JSONRPC string      `json:"jsonrpc"`
	ID      uint64      `json:"id"`
	Method  string      `json:"method"`
	Params  interface{} `json:"params,omitempty"`
}

// ToolCallParams defines parameters for tools/call
type ToolCallParams struct {
	Name      string                 `json:"name"`
	Arguments map[string]interface{} `json:"arguments"`
}

// JSONRPCResponse represents a JSON-RPC 2.0 response from an MCP server
type JSONRPCResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      uint64          `json:"id"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *JSONRPCError   `json:"error,omitempty"`
}

// JSONRPCError represents a standard JSON-RPC error
type JSONRPCError struct {
	Code    int             `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data,omitempty"`
}

func (e *JSONRPCError) Error() string {
	return fmt.Sprintf("mcp error (code %d): %s", e.Code, e.Message)
}

// ContentItem represents a content block returned by MCP tools
type ContentItem struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// ToolCallResult represents the payload inside JSON-RPC result
type ToolCallResult struct {
	Content []ContentItem `json:"content"`
	IsError bool          `json:"isError,omitempty"`
}

// Client interacts with Hivemind via Streamable HTTP (JSON-RPC 2.0)
type Client struct {
	endpoint        string
	token           string
	httpClient      *http.Client
	requestID       atomic.Uint64
	initMu          sync.Mutex
	initialized     bool
	sessionID       string
	protocolVersion string
}

// NewClient initializes a new MCP streamable HTTP client
func NewClient(endpoint string, token string, timeout time.Duration) *Client {
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	return &Client{
		endpoint: endpoint,
		token:    token,
		httpClient: &http.Client{
			Timeout: timeout,
		},
		protocolVersion: "2024-11-05",
	}
}

// SupportedMCPProtocols defines the strictly supported MCP protocol versions
var SupportedMCPProtocols = map[string]bool{
	"2024-11-05": true,
	"2024-10-07": true,
}

// Streamable HTTP media types per MCP specification
const (
	StreamableHTTPAccept = "application/json, text/event-stream"
)

// InitializeResult represents the expected JSON-RPC result from initialize
type InitializeResult struct {
	ProtocolVersion string             `json:"protocolVersion"`
	Capabilities    ServerCapabilities `json:"capabilities"`
	ServerInfo      ServerInfo         `json:"serverInfo"`
	Instructions    string             `json:"instructions,omitempty"`
}

// ServerCapabilities lists the MCP capabilities offered by the server
type ServerCapabilities struct {
	Tools     *ToolsCapability       `json:"tools,omitempty"`
	Resources *ResourcesCapability   `json:"resources,omitempty"`
	Prompts   *PromptsCapability     `json:"prompts,omitempty"`
	Logging   map[string]interface{} `json:"logging,omitempty"`
}

// ToolsCapability indicates support for MCP tools
type ToolsCapability struct {
	ListChanged bool `json:"listChanged,omitempty"`
}

// ResourcesCapability indicates support for MCP resources
type ResourcesCapability struct {
	Subscribe   bool `json:"subscribe,omitempty"`
	ListChanged bool `json:"listChanged,omitempty"`
}

// PromptsCapability indicates support for MCP prompts
type PromptsCapability struct {
	ListChanged bool `json:"listChanged,omitempty"`
}

// ServerInfo describes the remote MCP server
type ServerInfo struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// parseJSONRPCResponseBody decodes standard JSON-RPC response or SSE event stream
func parseJSONRPCResponseBody(contentType string, bodyBytes []byte) (*JSONRPCResponse, error) {
	trimmed := bytes.TrimSpace(bodyBytes)
	if strings.Contains(contentType, "text/event-stream") || bytes.HasPrefix(trimmed, []byte("data:")) || bytes.HasPrefix(trimmed, []byte("event:")) {
		lines := strings.Split(string(trimmed), "\n")
		var jsonBytes []byte
		for _, line := range lines {
			line = strings.TrimSpace(line)
			if strings.HasPrefix(line, "data:") {
				dataContent := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
				if len(dataContent) > 0 && dataContent != "[DONE]" {
					jsonBytes = []byte(dataContent)
					break
				}
			}
		}
		if len(jsonBytes) == 0 {
			return nil, fmt.Errorf("empty or invalid SSE event stream in response: %s", sanitizeErrSnippet(bodyBytes, 256))
		}
		var rpcResp JSONRPCResponse
		if err := json.Unmarshal(jsonBytes, &rpcResp); err != nil {
			return nil, fmt.Errorf("invalid json in SSE event data: %w", err)
		}
		return &rpcResp, nil
	}

	var rpcResp JSONRPCResponse
	if err := json.Unmarshal(bodyBytes, &rpcResp); err != nil {
		return nil, fmt.Errorf("invalid json-rpc response: %s", sanitizeErrSnippet(bodyBytes, 256))
	}
	return &rpcResp, nil
}

// ensureInitialized performs the MCP initialize handshake and initialized notification
func (c *Client) ensureInitialized(ctx context.Context) error {
	c.initMu.Lock()
	defer c.initMu.Unlock()

	if c.initialized {
		return nil
	}

	reqID := c.requestID.Add(1)
	initReq := JSONRPCRequest{
		JSONRPC: "2.0",
		ID:      reqID,
		Method:  "initialize",
		Params: map[string]interface{}{
			"protocolVersion": "2024-11-05",
			"capabilities": map[string]interface{}{
				"roots": map[string]interface{}{
					"listChanged": false,
				},
			},
			"clientInfo": map[string]interface{}{
				"name":    "hivemind-ingest",
				"version": "1.6.1",
			},
		},
	}

	reqBody, err := json.Marshal(initReq)
	if err != nil {
		return fmt.Errorf("failed to encode initialize request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(reqBody))
	if err != nil {
		return fmt.Errorf("failed to create initialize http request: %w", err)
	}

	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", StreamableHTTPAccept)
	if c.token != "" {
		httpReq.Header.Set("Authorization", "Bearer "+c.token)
	}

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("initialize http request to %s failed: %w", c.endpoint, err)
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		return fmt.Errorf("failed to read initialize response body: %w", err)
	}
	if int64(len(bodyBytes)) > maxResponseBytes {
		return fmt.Errorf("mcp initialize response exceeded maximum allowed size of %d bytes", maxResponseBytes)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("http error %d during initialize: %s", resp.StatusCode, sanitizeErrSnippet(bodyBytes, 256))
	}

	// Capture session header if provided
	if sid := resp.Header.Get("mcp-session-id"); sid != "" {
		c.sessionID = sid
	}

	rpcResp, err := parseJSONRPCResponseBody(resp.Header.Get("Content-Type"), bodyBytes)
	if err != nil {
		return fmt.Errorf("failed to parse initialize response: %w", err)
	}

	if rpcResp.JSONRPC != "2.0" {
		return fmt.Errorf("invalid jsonrpc version '%s' in initialize, expected '2.0'", rpcResp.JSONRPC)
	}
	if rpcResp.ID != reqID {
		return fmt.Errorf("mcp initialize response ID mismatch: expected %d, got %d", reqID, rpcResp.ID)
	}
	if rpcResp.Error != nil {
		return rpcResp.Error
	}

	// Decode and validate InitializeResult
	var initResult InitializeResult
	if err := json.Unmarshal(rpcResp.Result, &initResult); err != nil {
		return fmt.Errorf("failed to unmarshal initialize result: %w", err)
	}

	if !SupportedMCPProtocols[initResult.ProtocolVersion] {
		return fmt.Errorf("unsupported mcp protocol version '%s' (supported: 2024-11-05, 2024-10-07)", initResult.ProtocolVersion)
	}

	// If header is provided, it must be consistent with result
	if protoHeader := resp.Header.Get("mcp-protocol-version"); protoHeader != "" {
		if protoHeader != initResult.ProtocolVersion {
			return fmt.Errorf("mcp-protocol-version header '%s' contradicts initialize protocolVersion '%s'", protoHeader, initResult.ProtocolVersion)
		}
	}

	c.protocolVersion = initResult.ProtocolVersion

	if initResult.ServerInfo.Name == "" {
		return fmt.Errorf("server did not provide valid serverInfo in initialize result")
	}
	if initResult.Capabilities.Tools == nil {
		return fmt.Errorf("server does not declare 'tools' capability in initialize result")
	}

	// Send notification initialized
	notifReq := map[string]interface{}{
		"jsonrpc": "2.0",
		"method":  "notifications/initialized",
	}
	notifBody, err := json.Marshal(notifReq)
	if err != nil {
		return fmt.Errorf("failed to marshal notifications/initialized: %w", err)
	}
	notifHttpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(notifBody))
	if err != nil {
		return fmt.Errorf("failed to create notifications/initialized request: %w", err)
	}
	notifHttpReq.Header.Set("Content-Type", "application/json")
	notifHttpReq.Header.Set("Accept", StreamableHTTPAccept)
	if c.token != "" {
		notifHttpReq.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.sessionID != "" {
		notifHttpReq.Header.Set("mcp-session-id", c.sessionID)
	}
	if c.protocolVersion != "" {
		notifHttpReq.Header.Set("mcp-protocol-version", c.protocolVersion)
	}

	notifResp, err := c.httpClient.Do(notifHttpReq)
	if err != nil {
		return fmt.Errorf("notifications/initialized http request failed: %w", err)
	}
	defer notifResp.Body.Close()
	_, _ = io.Copy(io.Discard, notifResp.Body)

	if notifResp.StatusCode < 200 || notifResp.StatusCode >= 300 {
		return fmt.Errorf("notifications/initialized rejected with http status %d", notifResp.StatusCode)
	}

	c.initialized = true
	return nil
}

// CallTool invokes an MCP tool over Streamable HTTP (JSON-RPC 2.0)
func (c *Client) CallTool(ctx context.Context, toolName string, args map[string]interface{}) (map[string]interface{}, error) {
	if c.endpoint == "" {
		return nil, fmt.Errorf("mcp endpoint is not configured")
	}

	if err := c.ensureInitialized(ctx); err != nil {
		return nil, fmt.Errorf("mcp initialization failed: %w", err)
	}

	reqID := c.requestID.Add(1)
	rpcReq := JSONRPCRequest{
		JSONRPC: "2.0",
		ID:      reqID,
		Method:  "tools/call",
		Params: ToolCallParams{
			Name:      toolName,
			Arguments: args,
		},
	}

	reqBody, err := json.Marshal(rpcReq)
	if err != nil {
		return nil, fmt.Errorf("failed to encode request: %w", err)
	}

	// Measure the actual JSON-RPC envelope, including base64 and JSON escaping.
	if len(reqBody) > 75*1024*1024 {
		return nil, fmt.Errorf("MCP request exceeds 75 MiB; reduce the batch size")
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(reqBody))
	if err != nil {
		return nil, fmt.Errorf("failed to create http request: %w", err)
	}

	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", StreamableHTTPAccept)
	if c.token != "" {
		httpReq.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.sessionID != "" {
		httpReq.Header.Set("mcp-session-id", c.sessionID)
	}
	if c.protocolVersion != "" {
		httpReq.Header.Set("mcp-protocol-version", c.protocolVersion)
	}

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("http request to %s failed: %w", c.endpoint, err)
	}
	defer resp.Body.Close()

	// Read with limit+1 to strictly detect oversized responses
	bodyBytes, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}
	if int64(len(bodyBytes)) > maxResponseBytes {
		return nil, fmt.Errorf("mcp response exceeded maximum allowed size of %d bytes", maxResponseBytes)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("http error %d from server: %s", resp.StatusCode, sanitizeErrSnippet(bodyBytes, 256))
	}

	rpcResp, err := parseJSONRPCResponseBody(resp.Header.Get("Content-Type"), bodyBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to parse response: %w", err)
	}

	if rpcResp.JSONRPC != "2.0" {
		return nil, fmt.Errorf("invalid jsonrpc version '%s', expected '2.0'", rpcResp.JSONRPC)
	}

	if rpcResp.ID != reqID {
		return nil, fmt.Errorf("mcp response ID mismatch: expected %d, got %d", reqID, rpcResp.ID)
	}

	if rpcResp.Error != nil {
		return nil, rpcResp.Error
	}

	if len(rpcResp.Result) == 0 || string(rpcResp.Result) == "null" {
		return nil, fmt.Errorf("empty or null result in json-rpc response")
	}

	// 1. Try MCP ToolCallResult envelope (which has non-nil content array or isError == true)
	var toolRes ToolCallResult
	if err := json.Unmarshal(rpcResp.Result, &toolRes); err == nil && (toolRes.Content != nil || toolRes.IsError) {
		var combined string
		for _, item := range toolRes.Content {
			if item.Type == "text" {
				combined += item.Text
			}
		}
		combined = strings.TrimSpace(combined)

		if toolRes.IsError {
			if combined != "" {
				var parsed map[string]interface{}
				if err := json.Unmarshal([]byte(combined), &parsed); err == nil {
					parsed["status"] = "error"
					parsed["isError"] = true
					return parsed, nil
				}
				return map[string]interface{}{
					"status":  "error",
					"message": combined,
					"isError": true,
				}, nil
			}
			return map[string]interface{}{
				"status":  "error",
				"message": "mcp tool returned error (isError: true)",
				"isError": true,
			}, nil
		}

		if combined != "" {
			var parsed map[string]interface{}
			if err := json.Unmarshal([]byte(combined), &parsed); err == nil {
				return parsed, nil
			}
			return map[string]interface{}{
				"status":  "ok",
				"message": combined,
			}, nil
		}

		return map[string]interface{}{"status": "ok"}, nil
	}

	// 2. Direct map in Result (e.g. {"status":"ok", ...} or {"status":"error", ...})
	var directResult map[string]interface{}
	if err := json.Unmarshal(rpcResp.Result, &directResult); err == nil {
		return directResult, nil
	}

	return nil, fmt.Errorf("unrecognized mcp result format: %s", sanitizeErrSnippet(rpcResp.Result, 256))
}

func sanitizeErrSnippet(data []byte, maxLen int) string {
	s := strings.TrimSpace(string(data))
	if len(s) > maxLen {
		return s[:maxLen] + "..."
	}
	return s
}
