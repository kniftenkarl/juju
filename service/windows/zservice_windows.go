// mksyscall_windows.pl service_windows.go
// MACHINE GENERATED BY THE COMMAND ABOVE; DO NOT EDIT

package windows

import "unsafe"
import "syscall"
import "golang.org/x/sys/windows"

var (
	modadvapi32 = syscall.NewLazyDLL("advapi32.dll")

	procEnumServicesStatusW = modadvapi32.NewProc("EnumServicesStatusW")
)

func enumServicesStatus(h windows.Handle, dwServiceType uint32, dwServiceState uint32, lpServices uintptr, cbBufSize uint32, pcbBytesNeeded *uint32, lpServicesReturned *uint32, lpResumeHandle *uint32) (err error) {
	r1, _, e1 := syscall.Syscall9(procEnumServicesStatusW.Addr(), 8, uintptr(h), uintptr(dwServiceType), uintptr(dwServiceState), uintptr(lpServices), uintptr(cbBufSize), uintptr(unsafe.Pointer(pcbBytesNeeded)), uintptr(unsafe.Pointer(lpServicesReturned)), uintptr(unsafe.Pointer(lpResumeHandle)), 0)
	if r1 == 0 {
		if e1 != 0 {
			err = error(e1)
		} else {
			err = syscall.EINVAL
		}
	}
	return
}
