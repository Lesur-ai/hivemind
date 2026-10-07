//go:build linux

package tui

import "syscall"

func getTermiosIoctl() uint {
	return syscall.TCGETS
}
