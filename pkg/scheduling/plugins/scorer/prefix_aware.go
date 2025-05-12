package scorer

import (
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/plugins"
	"sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/types"
	logutil "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/util/logging"
)

const prefixAwareScorerName = "prefix-aware-scorer"

// PrefixAwareScorer is a routing scorer that scores pods based on the longest prefix match
// between the request's prompt and stored prefixes. The score is normalized between 0 and 1,
// where 1 represents the longest matching prefix.
type PrefixAwareScorer struct {
	prefixStore *PrefixStore
}

var _ plugins.Scorer = &PrefixAwareScorer{} // validate interface conformance

// NewPrefixAwareScorer creates a new PrefixAwareScorer with the given
// PrefixStoreConfig. If the config is nil, default is used.
func NewPrefixAwareScorer(config *PrefixStoreConfig) *PrefixAwareScorer {
	return &PrefixAwareScorer{
		prefixStore: NewPrefixStore(config),
	}
}

// Name returns the scorer's name
func (s *PrefixAwareScorer) Name() string {
	return "prefix-aware-scorer"
}

// Score scores the target pods based on the longest prefix match.
func (s *PrefixAwareScorer) Score(ctx *types.SchedulingContext, pods []types.Pod) map[types.Pod]float64 {
	loggerDebug := log.FromContext(ctx).WithName(prefixAwareScorerName).V(logutil.DEBUG)
	if ctx.Req == nil {
		loggerDebug.Info("Request is nil, skipping scoring")
		return nil
	}

	scores := s.prefixStore.FindMatchingPods(ctx.Req.Prompt, ctx.Req.TargetModel)
	loggerDebug.Info("Got pod scores", "scores", scores)

	if len(scores) == 0 {
		loggerDebug.Info("No scores found for pods")
		return nil
	}

	podToKey := func(pod types.Pod) (string, bool) {
		if pod.GetPod() == nil {
			return "", false
		}

		return pod.GetPod().NamespacedName.String(), true
	}

	return indexedScoresToNormalizedScoredPods(pods, podToKey, scores)
}

// PostSchedule implements the PostSchedulePlugin interface.
// It adds the prefix to the PrefixStore for the given pod.
// TODO: switch to PostResponse.
func (s *PrefixAwareScorer) PostSchedule(ctx *types.SchedulingContext, res *types.Result) {
	pod := res.TargetPod

	debugLogger := log.FromContext(ctx).WithName(prefixAwareScorerName)
	debugLogger.Info("PostResponse called", "req", ctx.Req, "pod", pod)

	if ctx.Req == nil {
		debugLogger.Info("Request is nil, skipping PostResponse")
		return
	}

	if pod.GetPod() == nil {
		debugLogger.Info("Pod is nil, skipping PostResponse", "req", ctx.Req, "pod", pod)
		return
	}

	if err := s.prefixStore.AddEntry(ctx.Req.TargetModel, ctx.Req.Prompt, &pod.GetPod().NamespacedName); err != nil {
		debugLogger.Error(err, "Failed to add entry to prefix store", "req", ctx.Req, "pod", pod)
		return
	}
}

// GetPrefixStore returns the scorer's PrefixStore.
func (s *PrefixAwareScorer) GetPrefixStore() *PrefixStore {
	return s.prefixStore
}
