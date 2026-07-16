{{/*
Expand the name of the chart.
*/}}
{{- define "hivepilot.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name. Truncated to 63 chars (k8s name limit).
*/}}
{{- define "hivepilot.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart name + version label.
*/}}
{{- define "hivepilot.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "hivepilot.labels" -}}
helm.sh/chart: {{ include "hivepilot.chart" . }}
{{ include "hivepilot.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Base selector labels, shared by every component.
*/}}
{{- define "hivepilot.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hivepilot.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component selector labels (api|scheduler|bot-telegram|bot-slack|bot-discord).
*/}}
{{- define "hivepilot.componentSelectorLabels" -}}
{{ include "hivepilot.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Resource name for a given component, e.g. "<fullname>-api".
*/}}
{{- define "hivepilot.componentName" -}}
{{- printf "%s-%s" (include "hivepilot.fullname" .context) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "hivepilot.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "hivepilot.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Image reference, honoring appVersion fallback when tag is unset.
*/}}
{{- define "hivepilot.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{/*
ConfigMap name for the rendered (or existing) non-secret config files.
*/}}
{{- define "hivepilot.configMapName" -}}
{{- if .Values.config.existingConfigMap -}}
{{- .Values.config.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "hivepilot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Secret name for the rendered (or existing) app secret.
*/}}
{{- define "hivepilot.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "hivepilot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
PVC name for persistent state.
*/}}
{{- define "hivepilot.pvcName" -}}
{{- if .Values.persistence.existingClaim -}}
{{- .Values.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-state" (include "hivepilot.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
volumeMounts that shadow the baked-in example config files at /app/<file>
with the operator-supplied ConfigMap content, one subPath mount per key
present in .Values.config.files (or all files in existingConfigMap, which we
cannot introspect — in that case the caller is expected to also set
.Values.config.files with the SAME keys so subPath mounts still get emitted,
even though the actual content comes from the existing ConfigMap at apply
time). Also mounts persistent state + (optionally) the tokens file.
*/}}
{{- define "hivepilot.configVolumeMounts" -}}
{{- range $filename, $content := .Values.config.files }}
- name: config
  mountPath: {{ printf "/app/%s" $filename }}
  subPath: {{ $filename }}
  readOnly: true
{{- end }}
{{- if or .Values.secrets.apiTokensYaml .Values.secrets.existingSecret }}
- name: tokens
  mountPath: /app/api_tokens.yaml
  subPath: api_tokens.yaml
  readOnly: true
{{- end }}
{{- end -}}

{{/*
Volumes backing hivepilot.configVolumeMounts above.
*/}}
{{- define "hivepilot.configVolumes" -}}
{{- if .Values.config.files }}
- name: config
  configMap:
    name: {{ include "hivepilot.configMapName" . }}
{{- end }}
{{- if or .Values.secrets.apiTokensYaml .Values.secrets.existingSecret }}
- name: tokens
  secret:
    secretName: {{ include "hivepilot.secretName" . }}
    items:
      - key: api_tokens.yaml
        path: api_tokens.yaml
{{- end }}
{{- end -}}

{{/*
Persistent state volume: PVC-backed when persistence.enabled or an
existingClaim is set, otherwise falls back to emptyDir (ephemeral — state.db
is lost on pod restart; fine for smoke-testing, not for real use).
*/}}
{{- define "hivepilot.stateVolume" -}}
- name: state
{{- if or .Values.persistence.enabled .Values.persistence.existingClaim }}
  persistentVolumeClaim:
    claimName: {{ include "hivepilot.pvcName" . }}
{{- else }}
  emptyDir: {}
{{- end }}
{{- end -}}
