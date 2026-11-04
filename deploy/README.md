# Deploy SANE Studio on Oracle Cloud (free)

SANE Studio runs on one Oracle Cloud *Always Free* Arm server, behind Caddy, which gets a free
HTTPS certificate for an address of the form `https://<ip-with-dashes>.sslip.io`. Nothing here
costs money as long as the account stays on the free tier and the server stays within the
Always Free allowance (Arm `VM.Standard.A1.Flex`: 2 OCPUs and 12 GB in total).

## 1. Create the server (Oracle console, once)

1. Sign up at <https://www.oracle.com/cloud/free/>. Pick the home region closest to your users;
   it cannot be changed later, and the free Arm servers run only there.
2. **Compute → Instances → Create instance**
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **Ampere → VM.Standard.A1.Flex**, **2 OCPUs, 12 GB memory**.
   - Networking: create a new virtual cloud network with a **public subnet**, and keep
     **Assign a public IPv4 address** on.
   - SSH keys: **Paste public keys**, and paste the content of `~/.ssh/sane_oracle.pub`.
   - Create. If Oracle reports *Out of host capacity*, choose another availability domain
     or try again later.
3. **Open the web ports.** On the instance page, open the subnet → its **Default Security
   List** → **Add Ingress Rules**: source CIDR `0.0.0.0/0`, IP protocol TCP, destination port
   range `80,443`.
4. Copy the instance's **Public IP address**.

## 2. Deploy (this PC, Git Bash)

```bash
cp deploy/server.env.example deploy/server.env   # then set SERVER_IP=<public IP>
deploy/oracle.sh setup                            # once: Docker + host firewall
deploy/oracle.sh deploy                           # prints https://<ip>.sslip.io
```

Run `deploy/oracle.sh deploy` again after every change; it uploads the folder and rebuilds.

## 3. Turn it off and on

| What | How | Effect |
|---|---|---|
| Take the site offline | `deploy/oracle.sh stop` | link stops working, server stays up |
| Bring it back | `deploy/oracle.sh start` | same link works again |
| Stop the whole server | Oracle console → instance → **Stop** (later **Start**) | the public IP stays assigned while stopped |
| Remove everything | Oracle console → instance → **Terminate** (and delete the boot volume) | link and server are gone |

`deploy/oracle.sh status` checks the containers and the public link; `deploy/oracle.sh logs`
shows recent logs.

## Notes

- Oracle may reclaim (stop) an Always Free server that stays almost idle for 7 days; start it
  again from the console and run `deploy/oracle.sh start`.
- Anyone with the link can upload EEG. Uploads are limited to 1 GB and every session is deleted
  60 minutes after its last use (`SANE_MAX_UPLOAD_MB`, `SANE_TTL_MINUTES` in the Dockerfile).
- `deploy/server.env` holds the server IP and is not committed.
