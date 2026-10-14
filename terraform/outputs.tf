output "cloud_run_service_url" {
  description = "Public HTTPS endpoint of the deployed Enterprise Support Agent."
  value       = google_cloud_run_v2_service.enterprise_support_agent.uri
}

output "secret_manager_resource_name" {
  description = "Secret Manager resource storing the Gemini API key."
  value       = google_secret_manager_secret.gemini_api_key.name
}
