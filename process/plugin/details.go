// Copyright 2015 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package plugin

import (
	"encoding/json"

	"github.com/juju/errors"
)

// LaunchDetails represents information about a process launched by a plugin.
type LaunchDetails struct {
	// ID is a unique string identifying the process to the plugin.
	ID string `json:"id"`
	// Status is the plugin-defined status of the process after launch.
	Status
}

// UnmarshalDetails de-serialized the provided data into a LaunchDetails.
func UnmarshalDetails(b []byte) (LaunchDetails, error) {
	var details LaunchDetails
	if err := json.Unmarshal(b, &details); err != nil {
		return details, errors.Annotate(err, "error parsing data for procdetails")
	}
	if err := details.Validate(); err != nil {
		return details, errors.Annotate(err, "invalid procdetails")
	}
	return details, nil

}

// Validate returns nil if this value is valid, and an error that satisfies
// IsValid if it is not.
func (p LaunchDetails) Validate() error {
	if p.ID == "" {
		e := errors.NewErr("ID cannot be empty")
		return validationErr{&e}
	}
	return p.Status.Validate()
}
