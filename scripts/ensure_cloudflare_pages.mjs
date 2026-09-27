// Idempotent bootstrap for the same Direct Upload flow used by eve_tools.
const token = process.env.CLOUDFLARE_API_TOKEN;
const account = process.env.CLOUDFLARE_ACCOUNT_ID;
if (!token || !account) {
  throw new Error("Configure CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in GitHub Actions secrets.");
}
const base = `https://api.cloudflare.com/client/v4/accounts/${encodeURIComponent(account)}/pages/projects`;
const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
let response = await fetch(`${base}/saygo`, { headers });
if (response.status === 404) {
  response = await fetch(base, {
    method: "POST",
    headers,
    body: JSON.stringify({ name: "saygo", production_branch: "main" }),
  });
}
const data = await response.json();
if (!response.ok || !data.success) {
  const codes = (data.errors || []).map(error => error.code).join(", ");
  throw new Error(`Cloudflare Pages setup failed (HTTP ${response.status}; codes: ${codes}). Check the account ID and Pages Edit permission.`);
}
if (data.result.production_branch !== "main") {
  throw new Error("Existing saygo Pages project has a different production branch; left unchanged.");
}
console.log(`Pages project ready: ${data.result.name} (${data.result.subdomain})`);
