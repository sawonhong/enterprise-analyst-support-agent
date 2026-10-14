variable "project_id" {
  description = "Google Cloud Project ID hosting the Enterprise Support Agent."
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run deployment."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Name of the Cloud Run v2 service."
  type        = string
  default     = "enterprise-support-agent"
}

variable "container_image" {
  description = "Artifact Registry container image URI."
  type        = string
  default     = "us-central1-docker.pkg.dev/enterprise-support/agents/support-agent:latest"
}
