terraform {
  required_version = ">= 1.13.5, < 2.0.0"
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "2.17.0"
    }
  }
}

# Configure this context using `az aks get-credentials` and kubelogin, not admin certificates.
provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}

variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "kube_context" {
  type        = string
  description = "Explicit existing AKS context. Never implicitly use the current context."
  validation {
    condition     = length(trimspace(var.kube_context)) > 0
    error_message = "An explicit nonempty AKS kubeconfig context is required."
  }
}

variable "namespace" {
  type    = string
  default = "archers"
}

variable "values_file" {
  type        = string
  description = "Absolute path to reviewed, non-secret Helm values for existing Azure resources."
}

variable "migration_revision_applied" {
  type        = string
  description = "Set only after the standalone migration Job has completed successfully."
}

module "archers" {
  source                     = "../modules/archers"
  namespace                  = var.namespace
  values_yaml                = file(var.values_file)
  migration_revision_applied = var.migration_revision_applied
}

output "release_name" {
  value = module.archers.release_name
}
