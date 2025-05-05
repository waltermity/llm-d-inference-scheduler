// Package filter provides filter plugins for the epp.
package filter

import (
	"fmt"

	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/plugins"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/types"
	logutil "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/util/logging"
)

// Passthrough filter type
type Passthrough struct{}

var _ plugins.Filter = &Passthrough{}

// Name returns the filter name
func (p *Passthrough) Name() string {
	return "passthrough-filter"
}

// Filter defines the filtering function. In this case it is a passthrough
func (p *Passthrough) Filter(ctx *types.SchedulingContext, pods []types.Pod) []types.Pod {
	ctx.Logger.V(logutil.DEBUG).Info(fmt.Sprintf("Passthrough filter called with %d candidates: %+v",
		len(pods), pods))

	return pods
}
