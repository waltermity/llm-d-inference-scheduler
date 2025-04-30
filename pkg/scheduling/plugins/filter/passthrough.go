package filter

import (
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/types"
)

// Passthrough filter type
type Passthrough struct{}

// Name returns the filter name
func (p *Passthrough) Name() string {
	return "passthrough-filter"
}

// Filter defines the filtering function. In this case it is a passthrough
func (p *Passthrough) Filter(ctx *types.SchedulingContext, pods []types.Pod) []types.Pod {
	return pods
}
