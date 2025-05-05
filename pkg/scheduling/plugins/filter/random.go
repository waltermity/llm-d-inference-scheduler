// Package filter provides filter plugins for the epp.
package filter

import (
	"fmt"
	"math/rand/v2"

	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/plugins"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/types"
	logutil "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/util/logging"
)

// Random drop filter type
type Random struct {
	probability float64
}

var _ plugins.Filter = &Random{}

// Name returns the filter name
func (r *Random) Name() string {
	return "random-drop-filter"
}

// Filter defines the filtering function. In this case it is a passthrough
func (r *Random) Filter(ctx *types.SchedulingContext, pods []types.Pod) []types.Pod {
	ctx.Logger.V(logutil.DEBUG).Info(fmt.Sprintf("Random filter called with %d candidates: %+v",
		len(pods), pods))
	filtered := []types.Pod{}

	for _, p := range pods {
		if rand.Float64() >= r.probability {
			filtered = append(filtered, p)
		} else {
			ctx.Logger.V(logutil.DEBUG).Info(fmt.Sprintf("%v dropped", p))
		}
	}

	return filtered
}
