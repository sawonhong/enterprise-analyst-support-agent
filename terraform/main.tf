# ==============================================================================
# Terraform Infrastructure as Code (IaC) for GCP Cloud Run & Secret Manager
# ==============================================================================
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "enterprise-support-gemini-api-key"
  replication {
    auto {}
  }
}

resource "google_cloud_run_v2_service" "enterprise_support_agent" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    containers {
      image = var.container_image
      ports {
        container_port = 8080
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "ROUTER_MODEL"
        value = "gemini-2.5-flash"
      }
      env {
        name  = "SYNTHESIS_MODEL"
        value = "gemini-2.5-flash"
      }
      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
    }
  }
}
