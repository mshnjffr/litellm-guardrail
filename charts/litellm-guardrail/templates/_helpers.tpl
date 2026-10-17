{{- define "piiService.selectorLabels" -}}
app.kubernetes.io/name: pii-service
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "piiService.labels" -}}
{{ include "piiService.selectorLabels" . }}
app.kubernetes.io/part-of: litellm-guardrail
app.kubernetes.io/version: {{ .Values.piiService.image.tag | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}
