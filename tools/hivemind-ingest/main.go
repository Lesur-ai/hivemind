package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"hivemind-ingest/internal/cli"
	"hivemind-ingest/internal/config"
	"hivemind-ingest/internal/tui"
)

var (
	Version   = "dev"
	Commit    = "dev"
	BuildDate = "unknown"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error: failed to load config file: %v\n", err)
		os.Exit(1)
	}

	ui := tui.NewUI(os.Stdin, os.Stdout)
	app := cli.NewAppWithBuildInfo(ui, cfg, Version, Commit, BuildDate)

	exitCode := app.Run(ctx, os.Args[1:])
	os.Exit(exitCode)
}
