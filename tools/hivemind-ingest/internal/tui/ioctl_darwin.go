//go:build darwin

package tui

import "syscall"

func getTermiosIoctl() uint {
	return syscall.TIOCGETA
}
