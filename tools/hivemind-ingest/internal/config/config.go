package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	DefaultDirMode  = 0700
	DefaultFileMode = 0600
)

// Config holds all parameters for hivemind-ingest
type Config struct {
	Endpoint          string   `yaml:"endpoint"`
	Token             string   `yaml:"token"`
	SpaceID           string   `yaml:"space_id"`
	DefaultOntology   string   `yaml:"default_ontology"`
	BatchSizeMB       int      `yaml:"batch_size_mb"`
	ThresholdOther    int      `yaml:"threshold_other"`
	AllowedExtensions []string `yaml:"allowed_extensions"`
	WatchJobs         bool     `yaml:"watch_jobs"`
	TimeoutSeconds    int      `yaml:"timeout_seconds"`
}

// DefaultConfig returns reasonable defaults
func DefaultConfig() *Config {
	return &Config{
		Endpoint:          "http://localhost:8000/mcp",
		Token:             "",
		SpaceID:           "default",
		DefaultOntology:   "",
		BatchSizeMB:       50,
		ThresholdOther:    25,
		AllowedExtensions: []string{".md", ".txt", ".json", ".yaml", ".yml", ".py", ".go", ".ts", ".js", ".pdf"},
		WatchJobs:         true,
		TimeoutSeconds:    600,
	}
}

// GetConfigPath returns the canonical path ~/.config/hivemind/config.yaml
func GetConfigPath() string {
	if custom := os.Getenv("HIVEMIND_CONFIG_PATH"); custom != "" {
		return custom
	}
	if custom := os.Getenv("HIVEMIND_CONFIG"); custom != "" {
		return custom
	}
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".config", "hivemind", "config.yaml")
}

// Load reads config from file, then overrides with environment variables
func Load() (*Config, error) {
	cfg := DefaultConfig()
	path := GetConfigPath()

	if _, err := os.Stat(path); err == nil {
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("failed to read config file %s: %w", path, err)
		}
		if err := yaml.Unmarshal(data, cfg); err != nil {
			return nil, fmt.Errorf("failed to parse config file %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return nil, fmt.Errorf("failed to access config file %s: %w", path, err)
	}

	// Environment variable overrides
	if env := os.Getenv("HIVEMIND_ENDPOINT"); env != "" {
		cfg.Endpoint = env
	}
	if env := os.Getenv("HIVEMIND_TOKEN"); env != "" {
		cfg.Token = env
	}
	if env := os.Getenv("HIVEMIND_SPACE_ID"); env != "" {
		cfg.SpaceID = env
	} else if env := os.Getenv("HIVEMIND_SPACE"); env != "" {
		cfg.SpaceID = env
	}
	if env := os.Getenv("HIVEMIND_ONTOLOGY"); env != "" {
		cfg.DefaultOntology = env
	}
	if env := os.Getenv("HIVEMIND_BATCH_SIZE_MB"); env != "" {
		if val, err := strconv.Atoi(env); err == nil && val > 0 {
			cfg.BatchSizeMB = val
		}
	}
	if env := os.Getenv("HIVEMIND_THRESHOLD_OTHER"); env != "" {
		if val, err := strconv.Atoi(env); err == nil && val >= 0 && val <= 100 {
			cfg.ThresholdOther = val
		}
	}
	if env := os.Getenv("HIVEMIND_EXTENSIONS"); env != "" {
		parts := strings.Split(env, ",")
		var exts []string
		for _, p := range parts {
			trimmed := strings.TrimSpace(p)
			if trimmed != "" {
				if !strings.HasPrefix(trimmed, ".") {
					trimmed = "." + trimmed
				}
				exts = append(exts, strings.ToLower(trimmed))
			}
		}
		if len(exts) > 0 {
			cfg.AllowedExtensions = exts
		}
	}
	if env := os.Getenv("HIVEMIND_TIMEOUT_SECONDS"); env != "" {
		if val, err := strconv.Atoi(env); err == nil && val > 0 {
			cfg.TimeoutSeconds = val
		}
	}

	return cfg, nil
}

// Save writes the configuration to disk with strict 0600 permissions atomically
func Save(cfg *Config) error {
	path := GetConfigPath()
	dir := filepath.Dir(path)

	if dirInfo, err := os.Stat(dir); os.IsNotExist(err) {
		if err := os.MkdirAll(dir, DefaultDirMode); err != nil {
			return fmt.Errorf("failed to create config directory %s: %w", dir, err)
		}
	} else if err != nil {
		return fmt.Errorf("failed to inspect config directory %s: %w", dir, err)
	} else if !dirInfo.IsDir() {
		return fmt.Errorf("config path parent %s is not a directory", dir)
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("failed to marshal config: %w", err)
	}

	tmpFile, err := os.CreateTemp(dir, ".config-*.tmp")
	if err != nil {
		return fmt.Errorf("failed to create temporary config file in %s: %w", dir, err)
	}
	tmpPath := tmpFile.Name()

	if err := os.Chmod(tmpPath, DefaultFileMode); err != nil {
		_ = tmpFile.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("failed to set 0600 permissions on %s: %w", tmpPath, err)
	}

	if _, err := tmpFile.Write(data); err != nil {
		_ = tmpFile.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("failed to write config data to %s: %w", tmpPath, err)
	}

	if err := tmpFile.Sync(); err != nil {
		_ = tmpFile.Close()
		_ = os.Remove(tmpPath)
		return fmt.Errorf("failed to sync config file %s: %w", tmpPath, err)
	}

	if err := tmpFile.Close(); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("failed to close temporary config file %s: %w", tmpPath, err)
	}

	if err := os.Rename(tmpPath, path); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("failed to atomically rename config file %s to %s: %w", tmpPath, path, err)
	}

	if err := os.Chmod(path, DefaultFileMode); err != nil {
		return fmt.Errorf("failed to set %v permissions on %s: %w", DefaultFileMode, path, err)
	}

	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("failed to open parent directory %s for sync: %w", dir, err)
	}
	if err := d.Sync(); err != nil {
		_ = d.Close()
		return fmt.Errorf("failed to sync parent directory %s: %w", dir, err)
	}
	if err := d.Close(); err != nil {
		return fmt.Errorf("failed to close parent directory %s: %w", dir, err)
	}

	return nil
}

// MaskToken returns a masked representation of the token for secure display
func MaskToken(token string) string {
	if token == "" {
		return "<unset>"
	}
	return "tok_***"
}
