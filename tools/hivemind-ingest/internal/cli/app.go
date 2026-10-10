package cli

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"hivemind-ingest/internal/config"
	"hivemind-ingest/internal/ingest"
	"hivemind-ingest/internal/mcpclient"
	"hivemind-ingest/internal/ontology"
	"hivemind-ingest/internal/tui"
)

// Exit codes
const (
	ExitSuccess          = 0
	ExitValidationError  = 1
	ExitNetworkError     = 2
	ExitIngestionFailure = 3
	ExitIngestionPending = 4
)

// App is the CLI application router
type App struct {
	ui        *tui.UI
	cfg       *config.Config
	version   string
	commit    string
	buildDate string
}

// NewApp creates a new CLI App with default version v1.6.1
func NewApp(ui *tui.UI, cfg *config.Config) *App {
	return NewAppWithBuildInfo(ui, cfg, "v1.6.1", "dev", "unknown")
}

// NewAppWithVersion creates a new CLI App with an explicit version string
func NewAppWithVersion(ui *tui.UI, cfg *config.Config, version string) *App {
	return NewAppWithBuildInfo(ui, cfg, version, "dev", "unknown")
}

// NewAppWithBuildInfo creates a new CLI App with explicit version, commit, and build date
func NewAppWithBuildInfo(ui *tui.UI, cfg *config.Config, version, commit, buildDate string) *App {
	if version == "" {
		version = "v1.6.1"
	}
	if commit == "" {
		commit = "dev"
	}
	if buildDate == "" {
		buildDate = "unknown"
	}
	return &App{ui: ui, cfg: cfg, version: version, commit: commit, buildDate: buildDate}
}

// Run executes the application given command-line arguments
func (a *App) Run(ctx context.Context, args []string) int {
	if len(args) < 1 {
		a.printUsage()
		return ExitValidationError
	}

	cmd := args[0]
	cmdArgs := args[1:]

	switch cmd {
	case "run":
		return a.handleRun(ctx, cmdArgs)
	case "test-ontology":
		return a.handleTestOntology(ctx, cmdArgs)
	case "config":
		return a.handleConfig(ctx, cmdArgs)
	case "help", "--help", "-h":
		a.printUsage()
		return ExitSuccess
	case "version", "--version", "-v":
		fmt.Printf("hivemind-ingest %s (commit: %s, date: %s)\n", a.version, a.commit, a.buildDate)
		return ExitSuccess
	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n", cmd)
		a.printUsage()
		return ExitValidationError
	}
}

func (a *App) printUsage() {
	a.ui.Banner()
	fmt.Println("Usage:")
	fmt.Println("  hivemind-ingest <command> [flags]")
	fmt.Println()
	fmt.Println("Commands:")
	fmt.Println("  run            Ingest documents into Hivemind Graph Memory")
	fmt.Println("  test-ontology  Evaluate ontology extraction fit against sample files")
	fmt.Println("  config         Manage server endpoint and authentication token")
	fmt.Println("  help           Display this help message")
	fmt.Println("  version        Display version information")
	fmt.Println()
	fmt.Println("Examples:")
	fmt.Println("  hivemind-ingest run --path ./docs --space my-team-space --ontology software --watch")
	fmt.Println("  hivemind-ingest test-ontology --path ./docs --ontology cloud --threshold-other 20")
	fmt.Println("  hivemind-ingest config set --endpoint https://hivemind.internal/mcp --token $HIVEMIND_TOKEN")
	fmt.Println("  hivemind-ingest config test")
}

func classifyError(err error) int {
	if err == nil {
		return ExitSuccess
	}
	s := strings.ToLower(err.Error())
	if strings.Contains(s, "http error") ||
		strings.Contains(s, "connection") ||
		strings.Contains(s, "connect:") ||
		strings.Contains(s, "network") ||
		strings.Contains(s, "mcp error") ||
		strings.Contains(s, "access denied") ||
		strings.Contains(s, "failed to verify or create space") ||
		strings.Contains(s, "failed to retrieve document catalog") ||
		strings.Contains(s, "timeout") {
		return ExitNetworkError
	}
	if strings.Contains(s, "required") ||
		strings.Contains(s, "requires --watch") ||
		strings.Contains(s, "automatic ontology requires at least one non-empty source") ||
		strings.Contains(s, "invalid") ||
		strings.Contains(s, "threshold_other must be") ||
		strings.Contains(s, "not found") ||
		strings.Contains(s, "syntax") {
		return ExitValidationError
	}
	return ExitIngestionFailure
}

func (a *App) handleRun(ctx context.Context, args []string) int {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)

	var (
		pathFlag        string
		spaceFlag       string
		ontologyFlag    string
		createSpaceFlag bool
		rulesFlag       string
		batchSizeMBFlag int
		extensionsFlag  string
		replaceFlag     bool
		watchFlag       bool
		noPollFlag      bool
		dryRunFlag      bool
		jsonFlag        bool
		nonInteractive  bool
		endpointFlag    string
		tokenFlag       string
		timeoutFlag     int
	)

	fs.StringVar(&pathFlag, "path", "", "Target file or directory to ingest")
	fs.StringVar(&pathFlag, "p", "", "Target file or directory (shorthand)")
	fs.StringVar(&spaceFlag, "space", a.cfg.SpaceID, "Target Hivemind space ID")
	fs.StringVar(&spaceFlag, "s", a.cfg.SpaceID, "Target Hivemind space ID (shorthand)")
	fs.StringVar(&ontologyFlag, "ontology", a.cfg.DefaultOntology, "Ontology name, YAML path, or experimental auto (requires --watch; supported delivery planned for 1.7.0)")
	fs.StringVar(&ontologyFlag, "o", a.cfg.DefaultOntology, "Ontology name, YAML path, or experimental auto (shorthand)")
	fs.BoolVar(&createSpaceFlag, "create-space-if-missing", false, "Automatically create the space if missing")
	fs.StringVar(&rulesFlag, "rules", "standard", "Rules template or Markdown file path if creating space (standard, or path to .md rules file)")
	fs.IntVar(&batchSizeMBFlag, "batch-size-mb", a.cfg.BatchSizeMB, "Max batch size in MB")
	fs.StringVar(&extensionsFlag, "extensions", "", "Comma-separated allowed extensions")
	fs.StringVar(&extensionsFlag, "x", "", "Comma-separated allowed extensions (shorthand)")
	fs.BoolVar(&replaceFlag, "replace", false, "Re-ingest documents even if SHA256 matches")
	fs.BoolVar(&watchFlag, "watch", true, "Wait for asynchronous background ingestion jobs to complete")
	fs.BoolVar(&noPollFlag, "no-poll", false, "Do not poll background job status")
	fs.BoolVar(&dryRunFlag, "dry-run", false, "Simulate ingestion without server calls")
	fs.BoolVar(&jsonFlag, "json", false, "Output results as formatted JSON")
	fs.BoolVar(&nonInteractive, "non-interactive", false, "Disable interactive prompts")
	fs.StringVar(&endpointFlag, "endpoint", a.cfg.Endpoint, "Override MCP endpoint URL")
	fs.StringVar(&tokenFlag, "token", a.cfg.Token, "Override auth token")
	fs.IntVar(&timeoutFlag, "timeout", a.cfg.TimeoutSeconds, "Execution timeout in seconds")

	if err := fs.Parse(args); err != nil {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": err.Error()})
		}
		return ExitValidationError
	}

	if noPollFlag {
		watchFlag = false
	}

	// Interactive Wizard if missing essential parameters and TTY available
	isInteractive := a.ui.IsInteractive() && !nonInteractive && !jsonFlag
	if isInteractive {
		if pathFlag == "" {
			a.ui.Banner()
			p, err := a.ui.Prompt("Enter target file or directory path", ".")
			if err != nil {
				return ExitValidationError
			}
			pathFlag = p
		}
		if spaceFlag == "" {
			s, err := a.ui.Prompt("Enter target Hivemind space ID", "default-space")
			if err != nil {
				return ExitValidationError
			}
			spaceFlag = s
		}
	}

	if pathFlag == "" {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": "--path is required"})
		} else {
			fmt.Fprintln(os.Stderr, "Error: --path is required")
		}
		return ExitValidationError
	}
	if spaceFlag == "" {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": "--space is required"})
		} else {
			fmt.Fprintln(os.Stderr, "Error: --space is required")
		}
		return ExitValidationError
	}

	endpoint := endpointFlag
	if endpoint == "" {
		endpoint = a.cfg.Endpoint
	}
	token := tokenFlag
	if token == "" {
		token = a.cfg.Token
	}

	if !dryRunFlag && endpoint == "" {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": "Hivemind endpoint is not configured"})
		} else {
			fmt.Fprintln(os.Stderr, "Error: Hivemind endpoint is not configured. Run 'hivemind-ingest config set --endpoint <url>' or set HIVEMIND_ENDPOINT.")
		}
		return ExitNetworkError
	}

	var allowedExts []string
	if extensionsFlag != "" {
		for _, e := range strings.Split(extensionsFlag, ",") {
			trimmed := strings.TrimSpace(e)
			if trimmed != "" {
				if !strings.HasPrefix(trimmed, ".") {
					trimmed = "." + trimmed
				}
				allowedExts = append(allowedExts, strings.ToLower(trimmed))
			}
		}
	} else {
		allowedExts = a.cfg.AllowedExtensions
	}

	if timeoutFlag <= 0 {
		timeoutFlag = a.cfg.TimeoutSeconds
	}
	if timeoutFlag <= 0 {
		timeoutFlag = 600
	}

	client := mcpclient.NewClient(endpoint, token, time.Duration(timeoutFlag)*time.Second)
	engine := ingest.NewEngine(client)

	opts := ingest.IngestOptions{
		Path:                 pathFlag,
		SpaceID:              spaceFlag,
		Ontology:             ontologyFlag,
		CreateSpaceIfMissing: createSpaceFlag,
		RulesTemplate:        rulesFlag,
		BatchSizeMB:          batchSizeMBFlag,
		AllowedExtensions:    allowedExts,
		ForceReplace:         replaceFlag,
		WatchJobs:            watchFlag,
		DryRun:               dryRunFlag,
		Timeout:              time.Duration(timeoutFlag) * time.Second,
	}

	var cb ingest.ProgressCallback
	if !jsonFlag {
		cb = func(stage string, message string, current int, total int, file string, jobID string, status string, errStr string) {
			if total > 0 {
				a.ui.ProgressBar(current, total, message)
			} else {
				fmt.Println(a.ui.Dim("»"), message)
			}
		}
	}

	res, err := engine.Run(ctx, opts, cb)
	if err != nil {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{
				"status": "error",
				"error":  err.Error(),
			})
		} else {
			fmt.Fprintf(os.Stderr, "%s %s\n", a.ui.Red("Error:"), err.Error())
		}
		return classifyError(err)
	}

	exitCode := ExitSuccess
	switch {
	case res.TotalFailed > 0:
		exitCode = ExitIngestionFailure
	case res.TotalPending > 0:
		exitCode = ExitIngestionPending
	case !res.Success:
		exitCode = ExitIngestionFailure
	}
	if jsonFlag {
		_ = a.ui.PrintJSON(res)
		return exitCode
	}

	fmt.Println()
	if res.TotalPending > 0 {
		fmt.Printf("%s Ingestion submitted (%d succeeded, %d pending, %d failed, %d not submitted); completion not verified.\n",
			a.ui.Yellow("!"), res.TotalSucceeded, res.TotalPending, res.TotalFailed, res.TotalNotSubmitted)
	} else if res.Success {
		fmt.Printf("%s Ingestion completed successfully in %d ms.\n", a.ui.Green("✓"), res.DurationMs)
		fmt.Printf("  Space: %s | Uploaded: %d | Skipped: %d | Total Succeeded: %d\n",
			a.ui.Bold(res.SpaceID), res.TotalUploaded, res.TotalSkipped, res.TotalSucceeded)
	} else {
		fmt.Printf("%s Ingestion finished with failures (%d succeeded, %d failed, %d not submitted).\n",
			a.ui.Red("✗"), res.TotalSucceeded, res.TotalFailed, res.TotalNotSubmitted)
	}
	return exitCode
}

func (a *App) handleTestOntology(ctx context.Context, args []string) int {
	fs := flag.NewFlagSet("test-ontology", flag.ContinueOnError)

	var (
		pathFlag       string
		ontologyFlag   string
		spaceFlag      string
		sampleSizeFlag int
		thresholdFlag  int
		keepMemoryFlag bool
		jsonFlag       bool
		endpointFlag   string
		tokenFlag      string
		timeoutFlag    int
	)

	fs.StringVar(&pathFlag, "path", ".", "Target file or directory of samples")
	fs.StringVar(&pathFlag, "p", ".", "Target file or directory (shorthand)")
	fs.StringVar(&ontologyFlag, "ontology", a.cfg.DefaultOntology, "Ontology name or YAML path")
	fs.StringVar(&ontologyFlag, "o", a.cfg.DefaultOntology, "Ontology name or YAML path (shorthand)")
	fs.StringVar(&spaceFlag, "space", "", "Optional space ID (ephemeral space created if omitted)")
	fs.IntVar(&sampleSizeFlag, "sample-size", 3, "Number of sample documents to evaluate")
	fs.IntVar(&thresholdFlag, "threshold-other", a.cfg.ThresholdOther, "Max allowable percentage of untyped 'Other' entities")
	fs.BoolVar(&keepMemoryFlag, "keep-memory", false, "Do not delete temporary evaluation space after testing")
	fs.BoolVar(&jsonFlag, "json", false, "Output results as JSON")
	fs.StringVar(&endpointFlag, "endpoint", a.cfg.Endpoint, "Override MCP endpoint URL")
	fs.StringVar(&tokenFlag, "token", a.cfg.Token, "Override auth token")
	fs.IntVar(&timeoutFlag, "timeout", a.cfg.TimeoutSeconds, "Timeout in seconds")

	if err := fs.Parse(args); err != nil {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": err.Error()})
		}
		return ExitValidationError
	}

	endpoint := endpointFlag
	if endpoint == "" {
		endpoint = a.cfg.Endpoint
	}
	token := tokenFlag
	if token == "" {
		token = a.cfg.Token
	}

	if timeoutFlag <= 0 {
		timeoutFlag = a.cfg.TimeoutSeconds
	}
	if timeoutFlag <= 0 {
		timeoutFlag = 600
	}

	client := mcpclient.NewClient(endpoint, token, time.Duration(timeoutFlag)*time.Second)
	eval := ontology.NewEvaluator(client)

	opts := ontology.EvalOptions{
		SpaceID:        spaceFlag,
		Ontology:       ontologyFlag,
		SampleSize:     sampleSizeFlag,
		ThresholdOther: thresholdFlag,
		KeepMemory:     keepMemoryFlag,
		AllowedExts:    a.cfg.AllowedExtensions,
		Timeout:        time.Duration(timeoutFlag) * time.Second,
	}

	res, err := eval.TestOntology(ctx, pathFlag, opts)
	if err != nil {
		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": err.Error()})
		} else {
			fmt.Fprintf(os.Stderr, "%s %s\n", a.ui.Red("Error:"), err.Error())
		}
		return classifyError(err)
	}

	if jsonFlag {
		_ = a.ui.PrintJSON(res)
		if res.PassedThreshold {
			return ExitSuccess
		}
		return ExitIngestionFailure
	}

	fmt.Println()
	a.ui.Banner()
	if res.PassedThreshold {
		fmt.Printf("%s %s\n", a.ui.Green("✓"), res.Message)
	} else {
		fmt.Printf("%s %s\n", a.ui.Yellow("⚠"), res.Message)
	}
	fmt.Printf("  Ontology: %s | Evaluated Files: %d | Space: %s\n",
		a.ui.Bold(res.OntologyPath), res.SampleCount, a.ui.Bold(res.SpaceID))
	fmt.Printf("  Relevance Tier: %s\n", a.ui.Bold(res.RelevanceTier))
	fmt.Printf("  Total Entities: %d (Typed: %d, Other: %d -> %.1f%%)\n",
		res.TotalEntities, res.TypedEntities, res.OtherEntities, res.OtherPercentage)

	if len(res.EntityTypesBreakdown) > 0 {
		fmt.Println("\n  Discovered Entity Types:")
		for tName, count := range res.EntityTypesBreakdown {
			fmt.Printf("    • %-20s: %d\n", tName, count)
		}
	}

	if len(res.UnclassifiedConcepts) > 0 {
		fmt.Println("\n  Unclassified 'Other' Concepts:")
		for _, concept := range res.UnclassifiedConcepts {
			fmt.Printf("    • %s\n", concept.Name)
		}
	}

	if len(res.Suggestions) > 0 {
		fmt.Println("\n  Recommendations:")
		for _, sug := range res.Suggestions {
			fmt.Printf("    → %s\n", sug)
		}
	}

	if res.IsTemporarySpace {
		if keepMemoryFlag || res.SpaceCleanupStatus == "retained" {
			if res.SpaceCleanupMessage != "" {
				fmt.Printf("\n  %s %s\n", a.ui.Dim("»"), res.SpaceCleanupMessage)
			} else {
				fmt.Printf("\n  %s Temporary space '%s' preserved (--keep-memory).\n", a.ui.Dim("»"), res.SpaceID)
			}
		} else if res.SpaceCleanupStatus == "failed" {
			fmt.Printf("\n  %s Warning: failed to clean up ephemeral space '%s': %s\n", a.ui.Yellow("!"), res.SpaceID, res.SpaceCleanupMessage)
		} else {
			fmt.Printf("\n  %s Ephemeral space '%s' cleaned up.\n", a.ui.Dim("»"), res.SpaceID)
		}
	}

	if res.PassedThreshold {
		return ExitSuccess
	}
	return ExitIngestionFailure
}

func (a *App) handleConfig(ctx context.Context, args []string) int {
	if len(args) < 1 {
		fmt.Println("Usage: hivemind-ingest config <set|get|test>")
		return ExitValidationError
	}

	subCmd := args[0]
	subArgs := args[1:]

	switch subCmd {
	case "get":
		var jsonFlag bool
		fs := flag.NewFlagSet("config get", flag.ContinueOnError)
		fs.BoolVar(&jsonFlag, "json", false, "Output config as JSON")
		if err := fs.Parse(subArgs); err != nil {
			return ExitValidationError
		}

		if jsonFlag {
			safeCfg := *a.cfg
			safeCfg.Token = config.MaskToken(safeCfg.Token)
			_ = a.ui.PrintJSON(safeCfg)
			return ExitSuccess
		}

		fmt.Printf("Config File: %s\n", config.GetConfigPath())
		fmt.Printf("  Endpoint:         %s\n", a.cfg.Endpoint)
		fmt.Printf("  Token:            %s\n", config.MaskToken(a.cfg.Token))
		fmt.Printf("  Default Space:    %s\n", a.cfg.SpaceID)
		fmt.Printf("  Default Ontology: %s\n", a.cfg.DefaultOntology)
		fmt.Printf("  Batch Size (MB):  %d\n", a.cfg.BatchSizeMB)
		fmt.Printf("  Threshold Other:  %d%%\n", a.cfg.ThresholdOther)
		fmt.Printf("  Allowed Exts:     %s\n", strings.Join(a.cfg.AllowedExtensions, ", "))
		fmt.Printf("  Timeout (s):      %d\n", a.cfg.TimeoutSeconds)
		return ExitSuccess

	case "set":
		fs := flag.NewFlagSet("config set", flag.ContinueOnError)
		var (
			endpointFlag    string
			tokenFlag       string
			spaceFlag       string
			ontologyFlag    string
			batchSizeMBFlag int
			thresholdFlag   int
			timeoutFlag     int
		)
		fs.StringVar(&endpointFlag, "endpoint", "", "Hivemind MCP endpoint URL")
		fs.StringVar(&tokenFlag, "token", "", "Authentication token")
		fs.StringVar(&spaceFlag, "space", "", "Default space ID")
		fs.StringVar(&ontologyFlag, "ontology", "", "Default ontology")
		fs.IntVar(&batchSizeMBFlag, "batch-size-mb", 0, "Max batch size in MB")
		fs.IntVar(&thresholdFlag, "threshold-other", -1, "Default Other threshold percentage")
		fs.IntVar(&timeoutFlag, "timeout", 0, "Default timeout in seconds")

		if err := fs.Parse(subArgs); err != nil {
			return ExitValidationError
		}

		if endpointFlag != "" {
			a.cfg.Endpoint = endpointFlag
		}
		if tokenFlag != "" {
			a.cfg.Token = tokenFlag
		}
		if spaceFlag != "" {
			a.cfg.SpaceID = spaceFlag
		}
		if ontologyFlag != "" {
			a.cfg.DefaultOntology = ontologyFlag
		}
		if batchSizeMBFlag > 0 {
			a.cfg.BatchSizeMB = batchSizeMBFlag
		}
		if thresholdFlag >= 0 && thresholdFlag <= 100 {
			a.cfg.ThresholdOther = thresholdFlag
		}
		if timeoutFlag > 0 {
			a.cfg.TimeoutSeconds = timeoutFlag
		}

		if err := config.Save(a.cfg); err != nil {
			fmt.Fprintf(os.Stderr, "%s Failed to save configuration: %v\n", a.ui.Red("Error:"), err)
			return ExitValidationError
		}
		fmt.Printf("%s Configuration saved to %s (permissions 0600)\n", a.ui.Green("✓"), config.GetConfigPath())
		return ExitSuccess

	case "test":
		var jsonFlag bool
		fs := flag.NewFlagSet("config test", flag.ContinueOnError)
		fs.BoolVar(&jsonFlag, "json", false, "Output test result as JSON")
		if err := fs.Parse(subArgs); err != nil {
			if jsonFlag {
				_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": err.Error()})
			}
			return ExitValidationError
		}

		if a.cfg.Endpoint == "" {
			if jsonFlag {
				_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": "Endpoint is not configured"})
			} else {
				fmt.Fprintln(os.Stderr, "Error: Endpoint is not configured.")
			}
			return ExitNetworkError
		}

		client := mcpclient.NewClient(a.cfg.Endpoint, a.cfg.Token, time.Duration(a.cfg.TimeoutSeconds)*time.Second)
		res, err := client.CallTool(ctx, "system_whoami", nil)
		if err != nil {
			if jsonFlag {
				_ = a.ui.PrintJSON(map[string]interface{}{"status": "error", "error": err.Error()})
			} else {
				fmt.Fprintf(os.Stderr, "%s Connection failed: %v\n", a.ui.Red("✗"), err)
			}
			return ExitNetworkError
		}

		if jsonFlag {
			_ = a.ui.PrintJSON(map[string]interface{}{"status": "ok", "whoami": res})
		} else {
			fmt.Printf("%s Connected to Hivemind MCP endpoint %s\n", a.ui.Green("✓"), a.cfg.Endpoint)
			if user, ok := res["user"].(string); ok && user != "" {
				fmt.Printf("  Authenticated as: %s\n", a.ui.Bold(user))
			}
		}
		return ExitSuccess

	default:
		fmt.Fprintf(os.Stderr, "Unknown config subcommand: %s\n", subCmd)
		return ExitValidationError
	}
}
