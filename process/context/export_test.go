// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package context

import (
	"github.com/juju/cmd"

	"github.com/juju/juju/process"
	"github.com/juju/juju/worker/uniter/runner/jujuc"
)

var (
	NewRegisterCommand = newRegisterCommand
)

func SetComponent(cmd cmd.Command, compCtx jujuc.ContextComponent) {
	switch cmd := cmd.(type) {
	case *RegisterCommand:
		cmd.compCtx = compCtx
	}
}

func AddProc(ctx *Context, id string, original *process.Info) {
	if err := ctx.addProc(id, original); err != nil {
		panic(err)
	}
}

func AddProcs(ctx *Context, procs ...*process.Info) {
	for _, proc := range procs {
		AddProc(ctx, proc.Name, proc)
	}
}
