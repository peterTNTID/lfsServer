# lfsServer

A minimal, self-hosted Git LFS server that stores objects in Google Cloud Storage.
Designed to run on Cloud Run with scale-to-zero.

## How It Works

- Implements the [Git LFS Batch API](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)
- Returns GCS **signed URLs** — the server never handles file data
- Uploads require an API key (HTTP Basic auth, key as password)
- Downloads are public (no auth required)
- Runs on Cloud Run, scales to zero when idle

### Architecture

```
git-lfs client
    │
    ▼
Cloud Run (this server)     ← handles only metadata (~1KB JSON)
    │
    ▼
GCS Signed URLs
    │
    ▼
Cloud Storage bucket        ← actual file data lives here
```

The server only processes small JSON metadata requests. All file data flows
directly between the git-lfs client and GCS via signed URLs, so there are no
timeout or memory concerns even with very large files.

## Prerequisites

- Google Cloud project with these APIs enabled:
  - Cloud Run (`run.googleapis.com`)
  - Cloud Storage (`storage.googleapis.com`)
  - Secret Manager (`secretmanager.googleapis.com`)
  - Artifact Registry (`artifactregistry.googleapis.com`)
  - IAM Credentials (`iamcredentials.googleapis.com`)
- `gcloud` CLI authenticated
- `gh` CLI (optional, for GitHub repo creation)

## Deployment

### 1. Create a GCS bucket

```bash
gcloud storage buckets create gs://YOUR-BUCKET \
  --project=YOUR-PROJECT \
  --location=YOUR-REGION \
  --uniform-bucket-level-access
```

### 2. Create an API key for write access

```bash
API_KEY=$(openssl rand -hex 32)

echo -n "$API_KEY" | gcloud secrets create lfs-write-api-key \
  --project=YOUR-PROJECT \
  --replication-policy="automatic" \
  --data-file=-

# Save this key — you'll need it for git credential setup
echo "Your API key: $API_KEY"
```

### 3. Set IAM permissions

The Cloud Run default service account needs three roles:

```bash
SA="YOUR-PROJECT-NUMBER-compute@developer.gserviceaccount.com"

# Required for signed URL generation on Cloud Run (critical!)
gcloud iam service-accounts add-iam-policy-binding $SA \
  --project=YOUR-PROJECT \
  --member="serviceAccount:$SA" \
  --role="roles/iam.serviceAccountTokenCreator"

# Storage access on the LFS bucket
gcloud storage buckets add-iam-policy-binding gs://YOUR-BUCKET \
  --member="serviceAccount:$SA" \
  --role="roles/storage.objectAdmin"

# Secret Manager access (also granted automatically by --set-secrets)
gcloud secrets add-iam-policy-binding lfs-write-api-key \
  --project=YOUR-PROJECT \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"
```

> **⚠️ The `serviceAccountTokenCreator` role is critical.** Without it, signed
> URL generation will fail with a cryptic error on Cloud Run. This is the most
> commonly missed step.

### 4. Deploy to Cloud Run

```bash
gcloud run deploy lfs-server \
  --project=YOUR-PROJECT \
  --region=YOUR-REGION \
  --source=. \
  --allow-unauthenticated \
  --set-secrets="LFS_WRITE_API_KEY=lfs-write-api-key:latest" \
  --set-env-vars="GCS_BUCKET=YOUR-BUCKET" \
  --min-instances=0 \
  --max-instances=3 \
  --memory=256Mi \
  --cpu=1 \
  --timeout=120 \
  --cpu-boost
```

Note the service URL from the output — you'll need it for `.lfsconfig`.

### 5. Configure your Git repo

Add a `.lfsconfig` file to your repository root:

```ini
[lfs]
    url = https://YOUR-CLOUD-RUN-URL
```

Configure credentials for pushing (only needed on machines that write):

```bash
git config --local lfs.https://YOUR-CLOUD-RUN-URL/.access basic

printf "protocol=https\nhost=YOUR-CLOUD-RUN-HOST\nusername=lfs-writer\npassword=YOUR-API-KEY\n\n" \
  | git credential-store store
```

No credentials are needed for cloning or pulling — downloads are public.

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GCS_BUCKET` | GCS bucket name for LFS objects | Yes |
| `LFS_WRITE_API_KEY` | API key for upload authorization | Yes |

## How Auth Works

- **Uploads**: The git-lfs client sends an `Authorization: Basic` header.
  The username is ignored; the password must match `LFS_WRITE_API_KEY`.
  If the key is missing or wrong, the server returns `401`.
- **Downloads**: No authentication. The server returns a time-limited GCS
  signed URL (1 hour expiry) for anyone to download.

## Object Storage Layout

Objects are stored in GCS with a fan-out directory structure:

```
gs://YOUR-BUCKET/lfs/objects/ab/cd/abcdef0123456789...
```

The first two pairs of hex characters from the OID are used as directory
prefixes to avoid performance issues with flat bucket listings.

## Cost

With scale-to-zero on Cloud Run, costs are essentially just storage:

| Storage | Cost |
|---------|------|
| 10 GB | ~$0.26/month |
| 100 GB | ~$2.60/month |
| 1 TB | ~$26/month |
| Cloud Run (scale-to-zero) | ~$0.00/month |

## Local Development

```bash
# Create a .env file from the template
cp .env.example .env
# Edit .env with your API key and bucket name

# Install dependencies
pip install -r requirements.txt

# Run locally
flask run --port 8080
```

## License

MIT — see [LICENSE](LICENSE).
