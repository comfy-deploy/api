# Studio URL Endpoint

## Overview

This new endpoint allows you to convert a deployment ID to the proper studio URL based on the current deployment environment.

## Endpoint

```
GET /api/share/deployment/{deployment_id}/studio-url
```

## Purpose

When you have a deployment ID but need to generate a link to the studio playground, this endpoint will:

1. Find the deployment's `share_slug` from the database
2. Determine the current environment based on the `CURRENT_API_URL` environment variable
3. Generate the appropriate studio URL based on the environment

## Request

**Path Parameters:**
- `deployment_id` (UUID): The ID of the deployment you want to get the studio URL for

**Example Request:**
```bash
curl -X GET "https://api.comfydeploy.com/api/share/deployment/123e4567-e89b-12d3-a456-426614174000/studio-url" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Response

**Success Response (200):**
```json
{
  "studio_url": "https://studio.comfydeploy.com/share/playground/comfy-deploy/hy3d-v21-all",
  "api_url": "https://api.comfydeploy.com"
}
```

**Response Fields:**
- `studio_url` (string): The complete studio URL for the deployment
- `api_url` (string): The current API URL being used

## Environment Examples

### Production
- API URL: `https://api.comfydeploy.com`
- Studio URL: `https://studio.comfydeploy.com/share/playground/{username}/{slug}`

### Staging  
- API URL: `https://staging.api.comfydeploy.com`
- Studio URL: `https://staging.studio.comfydeploy.com/share/playground/{username}/{slug}`

### Local Development
- API URL: `http://localhost:3011`
- Studio URL: `https://localhost:3011/share/playground/{username}/{slug}`

## Error Responses

**404 Not Found:**
```json
{
  "detail": "Deployment not found"
}
```

**400 Bad Request:**
```json
{
  "detail": "Deployment does not have a share slug. Only shared deployments can be accessed in studio."
}
```

**500 Internal Server Error:**
```json
{
  "detail": "CURRENT_API_URL environment variable not set"
}
```

## Use Case

This endpoint is particularly useful for:
- Discover page buttons that let users try styles in the playground
- Converting deployment IDs from output shares to studio URLs
- Programmatically generating studio links based on the current environment

## Implementation Notes

- The endpoint requires the deployment to have a `share_slug` (only shared deployments)
- The studio URL format follows the pattern: `https://{studio_domain}/share/playground/{username}/{slug_part}`
- The `share_slug` is split on the first underscore to extract username and slug parts
- Environment detection is based on the `CURRENT_API_URL` environment variable