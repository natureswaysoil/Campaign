# Amazon Ads Sheet Autopilot

This version reads products directly from your public Google Sheet CSV feed and adds:
- sheet-driven product loading
- auto keyword generation from `Keywords`, `Research_Keywords`, `Title`, and `Category`
- daily optimizer loop endpoint

---

## GitHub Actions — Required Secrets

The deploy workflow needs several GitHub secrets. Add them at:
**Repository → Settings → Secrets and variables → Actions → New repository secret**

### GCP Authentication (choose one option)

#### Option A — Workload Identity Federation (recommended)

Workload Identity Federation lets GitHub Actions authenticate to GCP without storing a long-lived key.

1. **Create a Workload Identity Pool and Provider** in your GCP project:

   ```bash
   PROJECT_ID="your-gcp-project-id"
   POOL_NAME="github-pool"
   PROVIDER_NAME="github-provider"
   REPO="your-github-org/your-repo"   # e.g. natureswaysoil/Campaign

   # Create the pool
   gcloud iam workload-identity-pools create "$POOL_NAME" \
     --project="$PROJECT_ID" \
     --location="global" \
     --display-name="GitHub Actions pool"

   # Create the OIDC provider
   gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
     --project="$PROJECT_ID" \
     --location="global" \
     --workload-identity-pool="$POOL_NAME" \
     --display-name="GitHub provider" \
     --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
     --issuer-uri="https://token.actions.githubusercontent.com"
   ```

2. **Create a service account** and grant it Cloud Run and Artifact Registry permissions:

   ```bash
   SA_NAME="github-deploy-sa"

   gcloud iam service-accounts create "$SA_NAME" \
     --project="$PROJECT_ID" \
     --display-name="GitHub Actions Deploy SA"

   SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/run.admin"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/artifactregistry.writer"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/iam.serviceAccountUser"
   ```

3. **Allow the GitHub repo to impersonate the service account**:

   ```bash
   POOL_ID=$(gcloud iam workload-identity-pools describe "$POOL_NAME" \
     --project="$PROJECT_ID" --location="global" \
     --format="value(name)")

   gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
     --project="$PROJECT_ID" \
     --role="roles/iam.workloadIdentityUser" \
     --member="principalSet://iam.googleapis.com/${POOL_ID}/attribute.repository/${REPO}"
   ```

4. **Retrieve the provider resource name** (this is the value for `GCP_WORKLOAD_IDENTITY_PROVIDER`):

   ```bash
   gcloud iam workload-identity-pools providers describe "$PROVIDER_NAME" \
     --project="$PROJECT_ID" \
     --location="global" \
     --workload-identity-pool="$POOL_NAME" \
     --format="value(name)"
   # Output looks like:
   # projects/123456789/locations/global/workloadIdentityPools/github-pool/providers/github-provider
   ```

5. **Add these two secrets to GitHub**:

   | Secret name | Value |
   |---|---|
   | `GCP_WORKLOAD_IDENTITY_PROVIDER` | Output of the `describe` command above |
   | `GCP_SERVICE_ACCOUNT_EMAIL` | `github-deploy-sa@<your-project>.iam.gserviceaccount.com` |

---

#### Option B — Service Account Key JSON (simpler, no WIF setup)

1. **Create a service account** and download a JSON key:

   ```bash
   PROJECT_ID="your-gcp-project-id"
   SA_NAME="github-deploy-sa"

   gcloud iam service-accounts create "$SA_NAME" \
     --project="$PROJECT_ID" \
     --display-name="GitHub Actions Deploy SA"

   SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/run.admin"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/artifactregistry.writer"

   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/iam.serviceAccountUser"

   gcloud iam service-accounts keys create key.json \
     --iam-account="$SA_EMAIL" \
     --project="$PROJECT_ID"
   ```

2. **Add one secret to GitHub**:

   | Secret name | Value |
   |---|---|
   | `GCP_SA_KEY` | Full contents of the `key.json` file |

   > ⚠️ Store `key.json` securely and delete it from your local machine after adding it to GitHub.

---

### Always-required secret

| Secret name | Value |
|---|---|
| `GCP_PROJECT_ID` | Your GCP project ID (e.g. `my-gcp-project-123`) |

---

### Amazon Ads secrets (loaded into Cloud Run via Secret Manager)

Store each of these in **both** GitHub Secrets (for validation) and **GCP Secret Manager** (for the running app).

| Secret name | Description |
|---|---|
| `AMAZON_ADS_CLIENT_ID` | Amazon Ads API client ID |
| `AMAZON_ADS_CLIENT_SECRET` | Amazon Ads API client secret |
| `AMAZON_ADS_REFRESH_TOKEN` | OAuth refresh token |
| `AMAZON_ADS_PROFILE_ID` | Amazon Ads profile ID |
| `AMAZON_ADS_REGION` | Region code: `na`, `eu`, or `fe` (defaults to `na` if unset) |

