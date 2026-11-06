# Deploy Vyral on Oracle Cloud Always Free

This runs Vyral on a free Oracle server, with a free HTTPS address. Oracle's screens and limits change, so check their current Always Free page as you go.

## Before you start

After you push to GitHub, open your repo's **Actions** tab and check that **Server image** passes. It builds the image on the same kind of ARM machine as Oracle's free servers and renders a test video. Fix any failure there before you set up a server.

## 1. Create the server

1. Sign up at https://www.oracle.com/cloud/free/. Choose your home region carefully, as you can't change it later. Pick one close to your Supabase project.
2. Go to **Compute**, then **Instances**, then **Create instance**.
3. Choose the image **Canonical Ubuntu 24.04**, and the shape **VM.Standard.A1.Flex** (Ampere ARM) with **2 OCPUs and 12 GB of memory**. Check that it's labelled **Always Free-eligible**. If capacity is unavailable, try another availability domain or try again later.
4. Save the SSH private key it offers, and create the instance. Note its **public IP address**.

## 2. Open the web ports

1. On the instance page, open its **virtual cloud network**, then its **security list**.
2. Add ingress rules allowing TCP ports **80** and **443** from `0.0.0.0/0`.

## 3. Install Docker on the server

Connect from your PC, using your key and IP address:

```bash
ssh -i path/to/your-key.key ubuntu@YOUR-IP
```

Then run these on the server, one at a time:

```bash
sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT
```

```bash
sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
```

```bash
sudo netfilter-persistent save
```

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker ubuntu
```

Then type `exit`, and connect with `ssh` again so the Docker permission takes effect.

## 4. Get the code and settings

```bash
git clone https://github.com/adithyaark/Vyral-Create.-Test.-Scale..git vyral
```

```bash
cd vyral
```

```bash
cp .env.example .env
```

```bash
chmod 600 .env
```

```bash
nano .env
```

Fill in `.env` with the same Supabase and API key values as on your PC, and add these lines. Use your server's IP address with dashes, plus `.sslip.io`, which gives you a free address without buying a domain:

```text
VYRAL_DOMAIN=vyral.YOUR-IP-WITH-DASHES.sslip.io
VYRAL_ADMIN_EMAILS=the-email-you-signed-up-with
```

For example, the IP `203.0.113.10` becomes `vyral.203-0-113-10.sslip.io`. Don't put a `VYRAL_PUBLIC_URL` line in this file, as Docker sets it from `VYRAL_DOMAIN`. If your database password contains `$`, change it first, because it can be misread.

## 5. Start Vyral

The first build can take 10 to 20 minutes.

```bash
docker compose up -d --build
```

Check that it's running. `vyral` should say `healthy` after about a minute:

```bash
docker compose ps
```

## 6. Update Supabase

In your Supabase project, open **Authentication**, then the URL settings. Set the **Site URL** to `https://YOUR-VYRAL-DOMAIN`, and add the same address to **Redirect URLs**. Keep `http://localhost:8765` in the redirect list if you still use Vyral on your PC.

Then open `https://YOUR-VYRAL-DOMAIN` and sign in.

## Everyday commands

See the logs:

```bash
docker compose logs -f vyral
```

Install an update after pushing to GitHub:

```bash
git pull && docker compose up -d --build
```

## Good to know

- **Bills:** only use resources labelled Always Free, and don't accept upgrade prompts unless you mean to pay.
- **Idle servers:** Oracle can reclaim Always Free servers that stay idle. Your projects and videos are in Supabase, so you'd only need to repeat these steps on a new server.
- **Speed:** with no graphics card, expect a video to take a few minutes. Jobs run one at a time.
- **Sign-ups:** anyone with the link can sign up. To stop this, turn off new sign-ups in Supabase's Authentication settings.
- **Emails:** Supabase's built-in email sender is rate-limited, so add your own email provider before inviting many people.
