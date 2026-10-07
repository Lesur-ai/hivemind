package tui

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
	"unsafe"
)

// UI manages terminal output and interactive prompts
type UI struct {
	in      io.Reader
	out     io.Writer
	reader  *bufio.Reader
	isTTY   bool
	noColor bool
}

// NewUI initializes terminal UI with automatic TTY and NO_COLOR detection
func NewUI(in io.Reader, out io.Writer) *UI {
	isTTY := false
	if f, ok := out.(*os.File); ok {
		isTTY = isTerminal(f.Fd())
	}
	noColor := os.Getenv("NO_COLOR") != "" || os.Getenv("TERM") == "dumb" || !isTTY

	return &UI{
		in:      in,
		out:     out,
		reader:  bufio.NewReader(in),
		isTTY:   isTTY,
		noColor: noColor,
	}
}

// IsInteractive returns true if running in an interactive terminal
func (u *UI) IsInteractive() bool {
	return u.isTTY
}

// Color formatting constants
const (
	colorReset   = "\033[0m"
	colorBold    = "\033[1m"
	colorDim     = "\033[2m"
	colorRed     = "\033[31m"
	colorGreen   = "\033[32m"
	colorYellow  = "\033[33m"
	colorBlue    = "\033[34m"
	colorMagenta = "\033[35m"
	colorCyan    = "\033[36m"
	colorWhite   = "\033[37m"
)

func (u *UI) style(text, code string) string {
	if u.noColor {
		return text
	}
	return code + text + colorReset
}

func (u *UI) Bold(text string) string   { return u.style(text, colorBold) }
func (u *UI) Dim(text string) string    { return u.style(text, colorDim) }
func (u *UI) Red(text string) string    { return u.style(text, colorRed) }
func (u *UI) Green(text string) string  { return u.style(text, colorGreen) }
func (u *UI) Yellow(text string) string { return u.style(text, colorYellow) }
func (u *UI) Cyan(text string) string   { return u.style(text, colorCyan) }

// Banner prints the stylish Hivemind CLI header
func (u *UI) Banner() {
	if u.noColor {
		fmt.Fprintln(u.out, "=== HIVEMIND LONG MEMORY INGESTION ENGINE ===")
		return
	}
	fmt.Fprintf(u.out, "%s%s⚡ HIVEMIND INGESTION ENGINE ⚡%s\n", colorBold, colorCyan, colorReset)
	fmt.Fprintf(u.out, "%sHigh-throughput asynchronous knowledge graph ingestion%s\n\n", colorDim, colorReset)
}

// PrintJSON outputs any struct as formatted JSON
func (u *UI) PrintJSON(v interface{}) error {
	enc := json.NewEncoder(u.out)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

// Prompt asks the user for text input with a default value
func (u *UI) Prompt(label, defaultVal string) (string, error) {
	promptText := label
	if defaultVal != "" {
		promptText = fmt.Sprintf("%s [%s]", label, u.Dim(defaultVal))
	}
	fmt.Fprintf(u.out, "%s: ", promptText)

	line, err := u.reader.ReadString('\n')
	if err != nil && err != io.EOF {
		return "", err
	}
	val := strings.TrimSpace(line)
	if val == "" {
		return defaultVal, nil
	}
	return val, nil
}

// Confirm asks a yes/no question
func (u *UI) Confirm(label string, defaultYes bool) (bool, error) {
	opts := "[y/N]"
	if defaultYes {
		opts = "[Y/n]"
	}
	fmt.Fprintf(u.out, "%s %s: ", label, u.Dim(opts))

	line, err := u.reader.ReadString('\n')
	if err != nil && err != io.EOF {
		return defaultYes, err
	}
	val := strings.ToLower(strings.TrimSpace(line))
	if val == "" {
		return defaultYes, nil
	}
	return val == "y" || val == "yes" || val == "o" || val == "oui", nil
}

// ProgressBar renders a progress indicator
func (u *UI) ProgressBar(current, total int, message string) {
	if !u.isTTY {
		fmt.Fprintf(u.out, "[%d/%d] %s\n", current, total, message)
		return
	}

	percent := 0
	if total > 0 {
		percent = (current * 100) / total
	}
	if percent > 100 {
		percent = 100
	}

	width := 24
	filled := (percent * width) / 100
	empty := width - filled

	bar := strings.Repeat("=", filled)
	if filled > 0 && empty > 0 {
		bar = bar[:filled-1] + ">"
	}
	space := strings.Repeat(" ", empty)

	fmt.Fprintf(u.out, "\r[%s%s] %3d%% (%d/%d) %-30s", u.Cyan(bar), space, percent, current, total, u.Dim(truncate(message, 30)))
	if current >= total {
		fmt.Fprintln(u.out)
	}
}

func truncate(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	if maxLen <= 3 {
		return s[:maxLen]
	}
	return s[:maxLen-3] + "..."
}

// isTerminal checks if fd is a real terminal on POSIX/Darwin
func isTerminal(fd uintptr) bool {
	var termios syscall.Termios
	_, _, err := syscall.Syscall6(
		syscall.SYS_IOCTL,
		fd,
		uintptr(getTermiosIoctl()),
		uintptr(unsafe.Pointer(&termios)),
		0, 0, 0,
	)
	return err == 0
}
