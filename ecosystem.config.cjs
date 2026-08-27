/** PM2: editorial pipeline + TG moderation bot */
module.exports = {
  apps: [
    {
      name: "max-repost-editorial",
      cwd: "/var/max-repost",
      script: "scripts/run-editorial.sh",
      interpreter: "bash",
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10_000,
      env: {
        PYTHONPATH: "/var/max-repost",
      },
    },
    {
      name: "max-repost-editorial-moderator",
      cwd: "/var/max-repost",
      script: "scripts/run-editorial-moderator.sh",
      interpreter: "bash",
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10_000,
      env: {
        PYTHONPATH: "/var/max-repost",
      },
    },
  ],
};
