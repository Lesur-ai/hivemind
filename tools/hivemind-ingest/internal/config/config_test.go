package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestConfigLoadAndSave(t *testing.T) {
	tempDir := t.TempDir()
	customPath := filepath.Join(tempDir, "test-config.yaml")

	t.Setenv("HIVEMIND_CONFIG_PATH", customPath)
	t.Setenv("HIVEMIND_ENDPOINT", "http://localhost:9999/mcp")
	t.Setenv("HIVEMIND_TOKEN", "test-secret-token")
	t.Setenv("HIVEMIND_SPACE_ID", "custom-space")
	t.Setenv("HIVEMIND_BATCH_SIZE_MB", "100")
	t.Setenv("HIVEMIND_TIMEOUT_SECONDS", "300")
	t.Setenv("HIVEMIND_THRESHOLD_OTHER", "15")
	t.Setenv("HIVEMIND_EXTENSIONS", ".txt,.md,.custom")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() failed: %v", err)
	}

	if cfg.Endpoint != "http://localhost:9999/mcp" {
		t.Errorf("expected endpoint http://localhost:9999/mcp, got %s", cfg.Endpoint)
	}
	if cfg.Token != "test-secret-token" {
		t.Errorf("expected token test-secret-token, got %s", cfg.Token)
	}
	if cfg.SpaceID != "custom-space" {
		t.Errorf("expected space custom-space, got %s", cfg.SpaceID)
	}
	if cfg.BatchSizeMB != 100 {
		t.Errorf("expected BatchSizeMB 100, got %d", cfg.BatchSizeMB)
	}
	if cfg.TimeoutSeconds != 300 {
		t.Errorf("expected TimeoutSeconds 300, got %d", cfg.TimeoutSeconds)
	}
	if cfg.ThresholdOther != 15 {
		t.Errorf("expected ThresholdOther 15, got %d", cfg.ThresholdOther)
	}
	if len(cfg.AllowedExtensions) != 3 {
		t.Errorf("expected 3 extensions, got %d", len(cfg.AllowedExtensions))
	}

	// Test Save (creates 0600 file)
	err = Save(cfg)
	if err != nil {
		t.Fatalf("Save() failed: %v", err)
	}

	info, err := os.Stat(customPath)
	if err != nil {
		t.Fatalf("stat failed: %v", err)
	}
	if info.Mode().Perm() != 0600 {
		t.Errorf("expected 0600 permissions, got %o", info.Mode().Perm())
	}

	// Test Atomic Save over existing file
	cfg.BatchSizeMB = 200
	err = Save(cfg)
	if err != nil {
		t.Fatalf("atomic Save() failed on existing file: %v", err)
	}
	info2, err := os.Stat(customPath)
	if err != nil || info2.Mode().Perm() != 0600 {
		t.Errorf("expected 0600 permissions after update, got %o (err=%v)", info2.Mode().Perm(), err)
	}

	// Test MaskToken
	masked := MaskToken("secret-bearer-token-12345")
	if masked != "tok_***" {
		t.Errorf("unexpected masked token: %s", masked)
	}
	if emptyMask := MaskToken(""); emptyMask != "<unset>" {
		t.Errorf("expected <unset> for empty token, got %s", emptyMask)
	}
}

func TestConfigSavePreservesExistingDirPermissions(t *testing.T) {
	tempDir := t.TempDir()
	customDir := filepath.Join(tempDir, "shared_dir")
	_ = os.MkdirAll(customDir, 0755)

	customPath := filepath.Join(customDir, "config.yaml")
	t.Setenv("HIVEMIND_CONFIG_PATH", customPath)

	cfg := &Config{
		Endpoint: "http://localhost:52379",
		SpaceID:  "test-space",
	}

	if err := Save(cfg); err != nil {
		t.Fatalf("Save failed: %v", err)
	}

	dirInfo, err := os.Stat(customDir)
	if err != nil {
		t.Fatalf("stat on customDir failed: %v", err)
	}
	if dirInfo.Mode().Perm() != 0755 {
		t.Errorf("expected existing directory permissions 0755 preserved, got %o", dirInfo.Mode().Perm())
	}

	fileInfo, err := os.Stat(customPath)
	if err != nil {
		t.Fatalf("stat on customPath failed: %v", err)
	}
	if fileInfo.Mode().Perm() != 0600 {
		t.Errorf("expected config file permissions 0600, got %o", fileInfo.Mode().Perm())
	}
}

func TestConfigLoadFailsClosedOnCorruptFile(t *testing.T) {
	tempDir := t.TempDir()
	customPath := filepath.Join(tempDir, "invalid-config.yaml")
	_ = os.WriteFile(customPath, []byte("endpoint: [invalid yaml structure"), 0600)

	t.Setenv("HIVEMIND_CONFIG_PATH", customPath)
	_, err := Load()
	if err == nil {
		t.Fatalf("expected Load() to fail closed on corrupt config file, got nil")
	}
}

func TestConfigLoadFailsClosedOnStatAccessError(t *testing.T) {
	tempDir := t.TempDir()
	inaccessibleDir := filepath.Join(tempDir, "no-access")
	_ = os.MkdirAll(inaccessibleDir, 0700)
	configPath := filepath.Join(inaccessibleDir, "config.yaml")
	_ = os.WriteFile(configPath, []byte("endpoint: http://localhost"), 0600)

	// Revoke permissions on directory to trigger os.Stat error
	_ = os.Chmod(inaccessibleDir, 0000)
	defer os.Chmod(inaccessibleDir, 0700)

	t.Setenv("HIVEMIND_CONFIG_PATH", configPath)
	_, err := Load()
	if err == nil {
		t.Fatalf("expected Load() to fail closed on stat access error, got nil")
	}
}
