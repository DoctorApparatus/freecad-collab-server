use std::time::Duration;

use anyhow::{bail, Context};
use freecad_collab_server::{
    build,
    config::{Config, LogFormat},
    protocol::{ClientMessage, ServerMessage, PROTOCOL_VERSION},
    util::{generate_token, hash_token, is_valid_user_name},
};
use tokio::net::TcpListener;
use tracing::info;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt, EnvFilter};

const USAGE: &str = "\
freecad-collab-server — real-time collaboration server for FreeCAD

USAGE:
    freecad-collab-server [--env-file <path>] [COMMAND]

COMMANDS:
    serve                         Run the server (default)
    migrate                       Apply database migrations and exit
    create-user <name> [--agent]  Create a user (or AI agent) and print an API token
    issue-token <name> [label]    Issue an additional API token for a user
    print-schema                  Print the WebSocket protocol JSON schema
    help                          Show this message

Configuration is read from the environment and from --env-file <path>
(default: ./.env if present). See .env.example for every variable.
Admin console: http://<host>:<port>/admin (needs COLLAB_ADMIN_TOKEN).";

const MAX_LOG_BYTES: u64 = 20 * 1024 * 1024;

fn open_log_file(path: &std::path::Path) -> anyhow::Result<std::fs::File> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    // Simple rotation: keep one previous file.
    if std::fs::metadata(path).map(|m| m.len() > MAX_LOG_BYTES).unwrap_or(false) {
        let _ = std::fs::rename(path, path.with_extension("log.1"));
    }
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("cannot open log file {}", path.display()))
}

fn init_tracing(config: &Config) -> anyhow::Result<()> {
    use std::io::IsTerminal;
    use tracing_subscriber::fmt::writer::BoxMakeWriter;

    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,tower_http=info,sqlx=warn"));
    let (writer, ansi) = match &config.log_file {
        Some(path) => (BoxMakeWriter::new(std::sync::Mutex::new(open_log_file(path)?)), false),
        None => (BoxMakeWriter::new(std::io::stdout), std::io::stdout().is_terminal()),
    };
    let layer = tracing_subscriber::fmt::layer().with_writer(writer).with_ansi(ansi);
    let registry = tracing_subscriber::registry().with(filter);
    match config.log_format {
        LogFormat::Json => registry.with(layer.json()).init(),
        LogFormat::Pretty => registry.with(layer).init(),
    }
    Ok(())
}

/// Pull `--env-file <path>` out of the arguments and load it.
fn load_env(args: &mut Vec<String>) -> anyhow::Result<()> {
    if let Some(i) = args.iter().position(|a| a == "--env-file") {
        let path = args
            .get(i + 1)
            .cloned()
            .context("--env-file needs a path")?;
        args.drain(i..=i + 1);
        dotenv::from_path(&path).with_context(|| format!("cannot read env file {path}"))?;
    } else {
        let _ = dotenv::dotenv();
    }
    Ok(())
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    load_env(&mut args)?;
    let command = args.first().map(String::as_str).unwrap_or("serve");

    if matches!(command, "help" | "--help" | "-h") {
        println!("{USAGE}");
        return Ok(());
    }
    if command == "print-schema" {
        let schema = serde_json::json!({
            "protocol_version": PROTOCOL_VERSION,
            "client_message": schemars::schema_for!(ClientMessage),
            "server_message": schemars::schema_for!(ServerMessage),
        });
        println!("{}", serde_json::to_string_pretty(&schema)?);
        return Ok(());
    }

    let config = Config::from_env()?;
    init_tracing(&config)?;

    match command {
        "serve" => serve(config).await,
        "migrate" => {
            let server = build(config).await?;
            server.pool.close().await;
            info!("migrations applied");
            Ok(())
        }
        "create-user" => {
            let name = args.get(1).context("usage: create-user <name> [--agent]")?;
            let kind = if args.iter().any(|a| a == "--agent") {
                freecad_collab_server::model::UserKind::Agent
            } else {
                freecad_collab_server::model::UserKind::Human
            };
            if !is_valid_user_name(name) {
                bail!("name must be 1-64 chars of [A-Za-z0-9_.-]");
            }
            let server = build(config).await?;
            let user = server.state.store.create_user(name, kind).await?;
            let token = generate_token();
            server
                .state
                .store
                .create_token(user.id, &hash_token(&token), Some("cli"))
                .await?;
            println!("user_id: {}\ntoken:   {token}", user.id);
            server.pool.close().await;
            Ok(())
        }
        "issue-token" => {
            let name = args.get(1).context("usage: issue-token <name> [label]")?;
            let label = args.get(2).map(String::as_str).unwrap_or("cli");
            let server = build(config).await?;
            let user = server
                .state
                .store
                .user_by_name(name)
                .await?
                .with_context(|| format!("no user named `{name}`"))?;
            let token = generate_token();
            let info = server
                .state
                .store
                .create_token(user.id, &hash_token(&token), Some(label))
                .await?;
            println!("token_id: {}\ntoken:    {token}", info.id);
            server.pool.close().await;
            Ok(())
        }
        other => bail!("unknown command `{other}`\n\n{USAGE}"),
    }
}

async fn serve(config: Config) -> anyhow::Result<()> {
    let addr = config.bind_addr;
    if config.admin_token.is_none() {
        info!("COLLAB_ADMIN_TOKEN not set: admin API disabled, use the CLI to manage users");
    }
    let server = build(config).await?;
    let listener = TcpListener::bind(addr)
        .await
        .with_context(|| format!("failed to bind {addr}"))?;
    info!(%addr, version = env!("CARGO_PKG_VERSION"), "listening");

    let shutdown = server.shutdown.clone();
    axum::serve(listener, server.router)
        .with_graceful_shutdown(async move {
            shutdown_signal().await;
            info!("shutdown requested; closing sessions");
            let _ = shutdown.send(true);
            // Give sessions a moment to send close frames.
            tokio::time::sleep(Duration::from_millis(250)).await;
        })
        .await?;

    server.pool.close().await;
    info!("stopped");
    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}
