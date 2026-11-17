{{- define "archers.name" -}}
{{- printf "%s-archers" .Release.Name | trunc 52 | trimSuffix "-" -}}
{{- end -}}

{{- define "archers.labels" -}}
app.kubernetes.io/name: archers
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end -}}

{{- define "archers.environment" -}}
- name: ARCHERS_CONFIG_FILE
  value: /run/config/settings.yaml
- name: ARCHERS_DATABASE_URL_FILE
  value: /run/secrets/database-url
- name: ARCHERS_OPERATOR_TOKEN_FILE
  value: /run/secrets/operator-token
- name: ARCHERS_SIGNING_KEY_FILE
  value: /run/secrets/signing-key
- name: HOME
  value: /tmp
{{- end -}}

{{- define "archers.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "archers.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "archers.settings" -}}
origins: [{{ .Values.application.origin | quote }}]
secure_cookie: true
network_mode: airgap
artifact_root: /data
model_path: /models/embedding
qdrant_url: {{ .Values.application.qdrantUrl | quote }}
max_pending_ingestion_jobs: {{ .Values.application.maxPendingIngestionJobs }}
max_upload_bytes: {{ .Values.application.maxUploadBytes }}
parser_timeout_seconds: {{ .Values.application.parserTimeoutSeconds }}
{{- if .Values.source.enabled }}
approved_mounts:
  inbox: /sources/inbox
network_mounts: [inbox]
mount_health_files:
  inbox: /run/source-health/inbox.json
{{- else }}
approved_mounts: {}
{{- end }}
{{- end -}}

{{- define "archers.migrationName" -}}
{{ include "archers.name" . | trunc 22 | trimSuffix "-" }}-migrate-{{ .Values.migration.revision }}
{{- end -}}

{{- define "archers.validate" -}}
{{- $_ := required "images.backend must be a digest-pinned image" .Values.images.backend -}}
{{- $_ := required "images.web must be a digest-pinned image" .Values.images.web -}}
{{- range $image := .Values.images -}}
  {{- if not (regexMatch "^.+@sha256:[a-f0-9]{64}$" $image) -}}
    {{- fail "Every image must be pinned with @sha256:<64 lowercase hex characters>" -}}
  {{- end -}}
{{- end -}}
{{- range $value := list .Values.existingSecret .Values.storage.artifactsClaim .Values.storage.modelClaim -}}
  {{- if not $value -}}{{- fail "existingSecret, storage.artifactsClaim and storage.modelClaim are required" -}}{{- end -}}
{{- end -}}
{{- if not (regexMatch "^https://[a-zA-Z0-9.-]+(:[0-9]+)?$" .Values.application.origin) -}}
  {{- fail "application.origin must be an exact HTTPS origin without a trailing slash" -}}
{{- end -}}
{{- if not (regexMatch "^https?://[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?(:[0-9]{1,5})?$" .Values.application.qdrantUrl) -}}
  {{- fail "application.qdrantUrl must be an operator-approved private HTTP(S) origin" -}}
{{- end -}}
{{- $qdrantPort := 80 -}}
{{- if hasPrefix "https://" .Values.application.qdrantUrl -}}{{- $qdrantPort = 443 -}}{{- end -}}
{{- if regexMatch ":[0-9]+$" .Values.application.qdrantUrl -}}
  {{- $qdrantPort = int (trimPrefix ":" (regexFind ":[0-9]+$" .Values.application.qdrantUrl)) -}}
{{- end -}}
{{- if ne $qdrantPort (int .Values.networkPolicy.qdrantPort) -}}
  {{- fail "networkPolicy.qdrantPort must match the Qdrant URL port" -}}
{{- end -}}
{{- if not .Values.networkPolicy.enabled -}}{{- fail "NetworkPolicy cannot be disabled in this chart" -}}{{- end -}}
{{- if or (empty .Values.networkPolicy.databaseCidrs) (empty .Values.networkPolicy.qdrantCidrs) -}}
  {{- fail "Explicit private databaseCidrs and qdrantCidrs are required" -}}
{{- end -}}
{{- range $cidr := concat .Values.networkPolicy.databaseCidrs .Values.networkPolicy.qdrantCidrs -}}
  {{- range $octet := splitList "." (first (splitList "/" $cidr)) -}}
    {{- if or (gt (int $octet) 255) (ne (printf "%d" (int $octet)) $octet) -}}{{- fail "Invalid IPv4 CIDR octet" -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- if and .Values.source.enabled (or (empty .Values.source.existingClaim) (empty .Values.source.healthClaim)) -}}
  {{- fail "Enabled source requires existingClaim and independently maintained healthClaim" -}}
{{- end -}}
{{- $scaled := false -}}
{{- range $component := list "api" "web" "worker" -}}
  {{- $config := index $.Values $component -}}
  {{- if or $config.autoscaling.enabled (gt (int $config.replicas) 1) -}}{{- $scaled = true -}}{{- end -}}
  {{- if gt (int $config.autoscaling.minReplicas) (int $config.autoscaling.maxReplicas) -}}
    {{- fail "HPA minReplicas must not exceed maxReplicas" -}}
  {{- end -}}
{{- end -}}
{{- if and $scaled (or (not .Values.storage.sharedArtifactsConfirmed) (not .Values.scaling.acknowledgeUncertified)) -}}
  {{- fail "Scaling requires sharedArtifactsConfirmed and acknowledgeUncertified after topology review" -}}
{{- end -}}
{{- $apiMax := int .Values.api.replicas -}}
{{- if .Values.api.autoscaling.enabled -}}{{- $apiMax = int .Values.api.autoscaling.maxReplicas -}}{{- end -}}
{{- $workerMax := int .Values.worker.replicas -}}
{{- if .Values.worker.autoscaling.enabled -}}{{- $workerMax = int .Values.worker.autoscaling.maxReplicas -}}{{- end -}}
{{- if gt $workerMax (int .Values.scaling.workerReplicaLimit) -}}{{- fail "Worker replicas exceed scaling.workerReplicaLimit" -}}{{- end -}}
{{- /* SQLAlchemy default pool_size=5 + max_overflow=10 per process; reserve one migration process. */ -}}
{{- $watcherCount := 0 -}}
{{- if .Values.source.enabled -}}{{- $watcherCount = 1 -}}{{- end -}}
{{- if gt (mul 15 (add $apiMax $workerMax $watcherCount 1)) (int .Values.scaling.databaseConnectionBudget) -}}
  {{- fail "Replica maxima exceed the conservative PostgreSQL connection budget (15 per backend process plus migration)" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
  {{- if or (not .Values.ingress.privateAccessConfirmed) (empty .Values.ingress.className) (empty .Values.ingress.tlsSecretName) (empty .Values.ingress.host) -}}
    {{- fail "Ingress requires privateAccessConfirmed, className, host and an existing TLS secret" -}}
  {{- end -}}
  {{- if ne .Values.application.origin (printf "https://%s" .Values.ingress.host) -}}
    {{- fail "Ingress host must match application.origin exactly (HTTPS port 443)" -}}
  {{- end -}}
  {{- if or (empty .Values.networkPolicy.ingressNamespaceLabels) (empty .Values.networkPolicy.ingressPodLabels) -}}
    {{- fail "Ingress requires explicit controller namespace and pod selectors" -}}
  {{- end -}}
{{- end -}}
{{- end -}}
