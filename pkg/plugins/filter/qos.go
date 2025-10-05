package filter

import (
    "context"
    "encoding/json"
    "fmt"
    "strings"

    "sigs.k8s.io/controller-runtime/pkg/log"
    "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/plugins"
    "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/framework"
    "sigs.k8s.io/gateway-api-inference-extension/pkg/epp/scheduling/types"
)

const (
    // QoSType is the plugin type name
    QoSType = "qos-filter"

    // header and label used by the filter
    qosHeader = "x-qos"
    qosLabel  = "llm-d.ai/qos"
)

type qosParameters struct {
    Header string `json:"header,omitempty"`
    Label  string `json:"label,omitempty"`
}

// compile-time type assertion
var _ framework.Filter = &QoSFilter{}

// QoSFactory constructs the QoS filter plugin
func QoSFactory(name string, rawParameters json.RawMessage, _ plugins.Handle) (plugins.Plugin, error) {
    params := qosParameters{
        Header: qosHeader,
        Label:  qosLabel,
    }
    if rawParameters != nil {
        if err := json.Unmarshal(rawParameters, &params); err != nil {
            return nil, fmt.Errorf("failed to parse %s plugin config: %w", QoSType, err)
        }
    }
    f := NewQoSFilter(params.Header, params.Label)
    return f.WithName(name), nil
}

// QoSFilter filters pods according to QoS header -> pod label mapping.
type QoSFilter struct {
    typedName plugins.TypedName
    header    string
    label     string
}

// NewQoSFilter creates a new QoSFilter
func NewQoSFilter(header, label string) *QoSFilter {
    return &QoSFilter{
        typedName: plugins.TypedName{Type: QoSType},
        header:    header,
        label:     label,
    }
}

// TypedName implements plugins.Plugin
func (f *QoSFilter) TypedName() plugins.TypedName { return f.typedName }

// WithName sets the plugin instance name
func (f *QoSFilter) WithName(name string) *QoSFilter {
    f.typedName.Name = name
    return f
}

// Filter filters pods based on request header value.
// If the header is missing/empty, the filter is a no-op (returns original pods).
// Pods whose label value equals the header value OR equals "both" are kept.
func (f *QoSFilter) Filter(ctx context.Context, _ *types.CycleState, request *types.LLMRequest, pods []types.Pod) []types.Pod {
    logger := log.FromContext(ctx).WithName(f.typedName.String())

    if request == nil || request.Headers == nil {
        logger.V(1).Info("request or headers nil, qos filter no-op")
        return pods
    }

    val := strings.ToLower(strings.TrimSpace(request.Headers[f.header]))
    if val == "" {
        logger.V(2).Info("qos header empty, qos filter no-op")
        return pods
    }

    filtered := make([]types.Pod, 0, len(pods))
    for _, p := range pods {
        mp := p.GetPod()
        if mp == nil {
            continue
        }
        l := strings.ToLower(strings.TrimSpace(mp.Labels[f.label]))
        if l == val || l == "both" {
            filtered = append(filtered, p)
        }
    }
    logger.Info("qos filter applied", "header", f.header, "value", val, "in", len(filtered), "out", len(pods))
    return filtered
}