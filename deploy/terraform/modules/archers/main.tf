terraform {
  required_version = ">= 1.13.5, < 2.0.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "2.17.0"
    }
  }
}

variable "namespace" {
  type        = string
  description = "Existing dedicated namespace with operator-managed Secrets and PVCs."
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.namespace))
    error_message = "Use a valid Kubernetes namespace."
  }
}

variable "release_name" {
  type    = string
  default = "archers"
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$", var.release_name))
    error_message = "Use a lowercase Helm release name of at most 40 characters."
  }
}

variable "values_yaml" {
  type        = string
  sensitive   = true
  description = "Non-secret chart values; only references to existing secrets are permitted."
  validation {
    condition     = can(yamldecode(var.values_yaml)) && !try(yamldecode(var.values_yaml).migration.enabled, false)
    error_message = "Supply valid chart YAML with migration.enabled=false. Run the separate migration gate first."
  }
}

variable "migration_revision_applied" {
  type        = string
  description = "Exact successful migration gate acknowledged before compatible application rollout."
  validation {
    condition     = var.migration_revision_applied == "0003_automatic_ingestion"
    error_message = "Apply and verify migration 0003_automatic_ingestion before deploying these images."
  }
}

locals {
  chart_path = abspath("${path.module}/../../../helm/archers")
  chart_hash = sha256(join("", [
    for path in sort(tolist(fileset(local.chart_path, "**"))) :
    "${path}:${filesha256("${local.chart_path}/${path}")}"
  ]))
}

resource "helm_release" "archers" {
  name             = var.release_name
  namespace        = var.namespace
  create_namespace = false
  chart            = local.chart_path
  description      = "Archers chart ${local.chart_hash}; schema ${var.migration_revision_applied}"
  values           = [var.values_yaml]
  timeout          = 900
  wait             = true
  atomic           = true
  cleanup_on_fail  = true
  max_history      = 5

  lifecycle {
    prevent_destroy = true
  }
}

output "release_name" {
  value = helm_release.archers.name
}

output "namespace" {
  value = helm_release.archers.namespace
}
