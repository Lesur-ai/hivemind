//go:build !darwin && !linux

package tui

func getTermiosIoctl() uint {
	return 0
}
