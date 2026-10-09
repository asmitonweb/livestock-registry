{{- define "openg2p-connector.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "openg2p-connector.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "openg2p-connector.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "openg2p-connector.labels" -}}
app.kubernetes.io/name: {{ include "openg2p-connector.name" . }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "openg2p-connector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "openg2p-connector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "openg2p-connector.serviceAccountName" -}}
{{- printf "%s-sa" (include "openg2p-connector.fullname" .) -}}
{{- end -}}

{{- define "openg2p-connector.serviceName" -}}
{{- printf "%s-%s" (include "openg2p-connector.fullname" .) . | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "openg2p-connector.commonEnv" -}}
- name: CONNECTOR_DB_DRIVER
  value: {{ .Values.commonEnv.CONNECTOR_DB_DRIVER | quote }}
- name: CONNECTOR_DB_HOSTNAME
  value: {{ .Values.global.postgresqlHost | quote }}
- name: CONNECTOR_DB_PORT
  value: {{ .Values.global.postgresqlPort | quote }}
- name: CONNECTOR_DB_USERNAME
  value: {{ .Values.global.connectorDBUser | quote }}
- name: CONNECTOR_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.connectorDBSecret | quote }}
      key: {{ .Values.global.connectorDBUserPasswordKey | quote }}
- name: CONNECTOR_DB_DBNAME
  value: {{ .Values.global.connectorDB | quote }}
- name: CONNECTOR_PARTNER_INGEST_BASE_URL
  value: {{ printf "http://%s.%s.svc.cluster.local" .Values.global.partnerApiService .Release.Namespace | quote }}
- name: CONNECTOR_CELERY_BROKER_URL
  value: {{ printf "redis://%s:%v/1" .Values.global.redisHost .Values.global.redisPort | quote }}
- name: CONNECTOR_CELERY_RESULT_BACKEND
  value: {{ printf "redis://%s:%v/1" .Values.global.redisHost .Values.global.redisPort | quote }}
- name: CONNECTOR_LOG_LEVEL
  value: {{ .Values.commonEnv.CONNECTOR_LOG_LEVEL | quote }}
- name: CONNECTOR_STORE_RUN_PAYLOADS
  value: {{ .Values.commonEnv.CONNECTOR_STORE_RUN_PAYLOADS | quote }}
- name: CONNECTOR_STRICT_INCREMENTAL
  value: {{ .Values.commonEnv.CONNECTOR_STRICT_INCREMENTAL | quote }}
- name: CONNECTOR_FULL_SCAN_ON_INCREMENTAL_UNSUPPORTED
  value: {{ .Values.commonEnv.CONNECTOR_FULL_SCAN_ON_INCREMENTAL_UNSUPPORTED | quote }}
- name: CONNECTOR_METRICS_ENABLED
  value: {{ .Values.commonEnv.CONNECTOR_METRICS_ENABLED | quote }}
- name: CONNECTOR_CORS_ORIGINS
  value: {{ .Values.commonEnv.CONNECTOR_CORS_ORIGINS | quote }}
{{- /*
Anything the allowlist above does not name. The list is fixed, so settings the
service reads straight from the environment -- CONNECTOR_ODK_CENTRAL_BASE_URL,
CONNECTOR_ODK_PROJECT_ID, CONNECTOR_ODK_FORM_ID, CONNECTOR_ODK_CENTRAL_EMAIL --
had no way through it: putting them under commonEnv rendered nothing at all,
silently, so default_pipelines.py could never seed a pipeline from values and
every environment had to be configured by hand in the UI.

extraEnvFrom carries the ones that must not appear in values, the ODK password
above all; its value is a verbatim env valueFrom block.
*/ -}}
{{- range $name, $value := .Values.extraEnv }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- range $name, $source := .Values.extraEnvFrom }}
- name: {{ $name }}
  valueFrom:
{{ toYaml $source | indent 4 }}
{{- end }}
{{- if .Values.global.metadataDsnSecret }}
- name: CONNECTOR_MASTER_DATA_DB_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.metadataDsnSecret | quote }}
      key: {{ .Values.global.masterDataDsnKey | quote }}
- name: CONNECTOR_REGISTRY_DB_DSN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.metadataDsnSecret | quote }}
      key: {{ .Values.global.registryDsnKey | quote }}
{{- end }}
{{- end -}}
