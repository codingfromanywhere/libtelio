//! Main and implementation of config and commands for Teliod - simple telio daemon for Linux and OpenWRT

use clap::Parser;
use nix::libc::SIGTERM;
use reqwest::StatusCode;
use signal_hook_tokio::Signals;
use std::{
    fs::{self, File},
    str::FromStr,
};
use thiserror::Error as ThisError;
use tokio::task::JoinError;
use tracing::{debug, error, info, level_filters::LevelFilter, trace, warn};

mod comms;
mod core_api;

use crate::comms::DaemonSocket;
use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use serde_json::error::Error as SerdeJsonError;
use std::cmp::min;
use telio::crypto::{PublicKey, SecretKey};
use telio::telio_model::config::Config as MeshMap;
use telio::{
    device::{Device, Error as DeviceError},
    telio_model::features::Features,
    telio_utils::select,
};
// Join this with above tokio
use tokio::sync::mpsc;
use tokio::time::Duration;

use crate::core_api::{
    get_meshmap, load_identifier_from_api, register_machine, update_machine, Error as ApiError,
};
use futures::stream::StreamExt;

const MAX_RETRIES: u32 = 5;
const BASE_DELAY_MS: u64 = 500;
const MAX_BACKOFF_TIME: u64 = 5000;

#[derive(Serialize, Deserialize, Debug, Clone)]
struct TeliodDaemonConfig {
    #[serde(
        deserialize_with = "deserialize_log_level",
        serialize_with = "serialize_log_level"
    )]
    log_level: LevelFilter,
    log_file_path: String,
    hw_identifier: Option<String>,
    authentication_token: String,
    private_key: Option<SecretKey>,
    machine_identifier: Option<String>,
}

#[derive(Deserialize, Debug, Default)]
struct Tokens {
    hw_identifier: String,
    auth_token: String,
    private_key: SecretKey,
    public_key: PublicKey,
    machine_identifier: String,
}

fn deserialize_log_level<'de, D>(deserializer: D) -> Result<LevelFilter, D::Error>
where
    D: Deserializer<'de>,
{
    Deserialize::deserialize(deserializer).and_then(|s: String| {
        LevelFilter::from_str(&s).map_err(|_| {
            de::Error::unknown_variant(&s, &["error", "warn", "info", "debug", "trace", "off"])
        })
    })
}

fn serialize_log_level<S>(level: &LevelFilter, serializer: S) -> Result<S::Ok, S::Error>
where
    S: Serializer,
{
    let level_str = match level {
        &LevelFilter::ERROR => "error",
        &LevelFilter::WARN => "warn",
        &LevelFilter::INFO => "info",
        &LevelFilter::DEBUG => "debug",
        &LevelFilter::TRACE => "trace",
        &LevelFilter::OFF => "off",
    };

    serializer.serialize_str(level_str)
}

#[derive(Parser, Debug)]
#[clap()]
#[derive(Serialize, Deserialize)]
enum ClientCmd {
    #[clap(
        about = "Forces daemon to add a log, added for testing to be, to be removed when the daemon will be more mature"
    )]
    HelloWorld { name: String },
    #[clap(about = "Fetches and update meshmap")]
    GetMeshmap,
}

#[derive(Parser, Debug)]
#[clap()]
enum Cmd {
    #[clap(about = "Runs the teliod event loop")]
    Daemon { config_path: String },
    #[clap(flatten)]
    Client(ClientCmd),
}

#[derive(Debug)]
enum TelioTaskCmd {
    UpdateMeshmap,
}

#[derive(Debug, ThisError)]
enum TeliodError {
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error("Invalid command received: {0}")]
    InvalidCommand(String),
    #[error("Broken signal stream")]
    BrokenSignalStream,
    #[error(transparent)]
    TelioTaskError(#[from] JoinError),
    #[error(transparent)]
    ParsingError(#[from] SerdeJsonError),
    #[error("Command failed to execute: {0:?}")]
    CommandFailed(ClientCmd),
    #[error("Daemon is not running")]
    DaemonIsNotRunning,
    #[error("Daemon is running")]
    DaemonIsRunning,
    #[error("Token not fully loaded")]
    TokenError,
    #[error(transparent)]
    CoreApiError(#[from] ApiError),
    #[error(transparent)]
    DeviceError(#[from] DeviceError),
}

#[tokio::main]
#[allow(large_futures)]
async fn main() -> Result<(), TeliodError> {
    match Cmd::parse() {
        Cmd::Daemon { config_path } => {
            if DaemonSocket::get_ipc_socket_path()?.exists() {
                Err(TeliodError::DaemonIsRunning)
            } else {
                let file = File::open(&config_path)?;
                let config: TeliodDaemonConfig = serde_json::from_reader(file)?;
                daemon_event_loop(config, config_path).await
            }
        }
        Cmd::Client(cmd) => {
            let socket_path = DaemonSocket::get_ipc_socket_path()?;
            if socket_path.exists() {
                let response =
                    DaemonSocket::send_command(&socket_path, &serde_json::to_string(&cmd)?).await?;

                if response.as_str() == "OK" {
                    println!("Command executed successfully");
                    Ok(())
                } else {
                    Err(TeliodError::CommandFailed(cmd))
                }
            } else {
                Err(TeliodError::DaemonIsNotRunning)
            }
        }
    }
}

struct CommandListener {
    socket: DaemonSocket,
}

impl CommandListener {
    async fn get_command(&self) -> Result<ClientCmd, TeliodError> {
        let mut connection = self.socket.accept().await?;
        let command_str = connection.read_command().await?;

        if let Ok(cmd) = serde_json::from_str::<ClientCmd>(&command_str) {
            connection.respond("OK".to_owned()).await?;
            Ok(cmd)
        } else {
            connection.respond("ERR".to_owned()).await?;
            Err(TeliodError::InvalidCommand(command_str))
        }
    }
}

async fn init_api(config: &mut TeliodDaemonConfig, config_path: String) -> Result<(), TeliodError> {
    let mut tokens = fetch_tokens(config).await;

    let mut retries = 0;
    loop {
        let status = update_machine(&tokens).await?;
        match status {
            StatusCode::OK => break,
            StatusCode::FORBIDDEN | StatusCode::UNAUTHORIZED | StatusCode::BAD_REQUEST => {
                // exit daemon
                panic!("{:?} response recieved. Exiting", status);
            }
            StatusCode::NOT_FOUND => {
                // Retry machine update after registering. To make sure everything was successful
                // If registering fails. Close the daemon
                if register_machine(&mut tokens).await.is_err() {
                    panic!("Unable to register machien. Exiting");
                }
            }
            _ => {
                // Retry with exp back-off
                if retries < MAX_RETRIES {
                    let backoff_time = BASE_DELAY_MS * 2_u64.pow(retries);
                    let sleep_time = min(backoff_time, MAX_BACKOFF_TIME);
                    debug!("Retrying after {} ms...", sleep_time);
                    tokio::time::sleep(Duration::from_millis(sleep_time)).await;
                    retries += 1;
                } else {
                    // If max retries exceeded, exit daemon
                    panic!("Max retries reached. Exiting.");
                }
            }
        }
    }

    config.private_key = Some(tokens.private_key.clone());
    config.hw_identifier = Some(tokens.hw_identifier.clone());
    config.machine_identifier = Some(tokens.machine_identifier.clone());

    let mut file = File::create(config_path)?;
    serde_json::to_writer(&mut file, &config)?;
    Ok(())
}

// From async context Telio needs to be run in separate task
fn telio_task(
    tokens: Tokens,
    mut rx_channel: mpsc::Receiver<TelioTaskCmd>,
) -> Result<(), TeliodError> {
    debug!("Initializing telio device");
    let telio = Device::new(
        Features::default(),
        // TODO: replace this with some real event handling
        move |event| info!("Incoming event: {:?}", event),
        None,
    )?;

    // TODO: This is temporary to be removed later on when we have proper integration
    // tests with core API. This is to not look for tokens in a test environment
    // right now as the values are dummy and program will not run as it expects
    // real tokens.
    if !tokens.auth_token.eq("") {
        if let Err(e) = update_meshmap(&tokens, &telio) {
            error!("Unable to set meshmap due to {e}");
        }

        while let Some(cmd) = rx_channel.blocking_recv() {
            info!("Got command {:?}", cmd);
            match cmd {
                TelioTaskCmd::UpdateMeshmap => {
                    if let Err(e) = update_meshmap(&tokens, &telio) {
                        error!("Unable to set meshmap due to {e}");
                    }
                }
            }
        }
    }
    Ok(())
}

fn update_meshmap(tokens: &Tokens, telio: &Device) -> Result<(), TeliodError> {
    let meshmap: MeshMap = serde_json::from_str(&get_meshmap(tokens)?)?;
    trace!("Meshmap {:#?}", meshmap);
    telio.set_config(&Some(meshmap))?;
    Ok(())
}

async fn fetch_tokens(config: &mut TeliodDaemonConfig) -> Tokens {
    info!("Fetching tokens");
    let mut tokens: Tokens = Tokens {
        auth_token: config.authentication_token.clone(),
        ..Default::default()
    };

    // Copy or generate Secret/public key pair
    if let Some(sk) = config.private_key {
        tokens.private_key = sk;
    } else {
        debug!("Generating secret key!");
        tokens.private_key = SecretKey::gen();
    }
    tokens.public_key = tokens.private_key.public();

    // Copy or generate hw_identifier
    if let Some(hw_id) = &config.hw_identifier {
        tokens.hw_identifier = hw_id.to_string();
    } else {
        debug!("Generating hw identifier");
        tokens.hw_identifier = format!("{}.{}", tokens.public_key, "openWRT");
    }

    // Copy or generate machine_identifier
    if let Some(machine_id) = &config.machine_identifier {
        tokens.machine_identifier = machine_id.to_string();
    } else {
        let identifier_loaded = load_identifier_from_api(&mut tokens).await;
        if identifier_loaded.is_err() {
            debug!("Machine not yet registered");
            // If registering fails. Close the daemon
            if register_machine(&mut tokens).await.is_err() {
                panic!("Unable to register machine. Exiting");
            }
        }
    }

    tokens
}

async fn daemon_event_loop(
    mut config: TeliodDaemonConfig,
    config_path: String,
) -> Result<(), TeliodError> {
    let (non_blocking_writer, _tracing_worker_guard) =
        tracing_appender::non_blocking(fs::File::create(&(config.log_file_path))?);
    tracing_subscriber::fmt()
        .with_max_level(config.log_level)
        .with_writer(non_blocking_writer)
        .with_ansi(false)
        .with_line_number(true)
        .with_level(true)
        .init();

    let socket = DaemonSocket::new(&DaemonSocket::get_ipc_socket_path()?)?;
    let cmd_listener = CommandListener { socket };

    // Tx is unused here, but this channel can be used to communicate with the
    // telio task
    let (tx, rx) = mpsc::channel(10);

    // TODO: This is temporary to be removed later on when we have proper integration
    // tests with core API. This is to not look for tokens in a test environment
    // right now as the values are dummy and program will not run as it expects
    // real tokens.
    let mut tokens = Tokens::default();
    if !config.authentication_token.eq("abcd1234") {
        init_api(&mut config, config_path).await?;

        tokens = Tokens {
            hw_identifier: config.hw_identifier.ok_or(TeliodError::TokenError)?,
            auth_token: config.authentication_token,
            machine_identifier: config.machine_identifier.ok_or(TeliodError::TokenError)?,
            private_key: config.private_key.ok_or(TeliodError::TokenError)?,
            public_key: config.private_key.ok_or(TeliodError::TokenError)?.public(),
        };
    }
    let telio_task_handle = tokio::task::spawn_blocking(|| telio_task(tokens, rx));
    let mut signals = Signals::new([SIGTERM])?;

    info!("Entering event loop");

    let result = loop {
        select! {
            maybe_cmd = cmd_listener.get_command() => {
                match maybe_cmd {
                    Ok(ClientCmd::HelloWorld{name}) => {
                        info!("Hello {}", name);
                    }
                    Ok(ClientCmd::GetMeshmap) => {
                        if let Err(e) = tx.try_send(TelioTaskCmd::UpdateMeshmap) {
                            error!("Channel closed or telio not yet started. {e}");
                        };
                    }
                    Err(TeliodError::InvalidCommand(cmd)) => {
                        warn!("Received invalid command from client: {}", cmd);
                    }
                    Err(error) => {
                        break Err(error);
                    }
                }
            },
            signal = signals.next() => {
                match signal {
                    Some(SIGTERM) => {
                        info!("Received SIGTERM signal, exiting");
                        fs::remove_file("/var/run/teliod.sock")?;
                        break Ok(());
                    }
                    Some(_) => {
                        info!("Received unexpected signal, ignoring");
                    }
                    None => {
                        break Err(TeliodError::BrokenSignalStream);
                    }
                }
            }
        }
    };

    // Wait until Telio task ends
    // TODO: When it will be doing something some channel with commands etc. might be needed
    let join_result = telio_task_handle.await?;

    if result.is_err() {
        result
    } else {
        join_result.map_err(|err| err.into())
    }
}
