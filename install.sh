#! /bin/bash

. ./config

POSITIONAL=()
while [[ $# -gt 0 ]]; do
        key="$1"

        case $key in
        -u | --username)
                USER_NAME="$2"
                shift # past argument
                shift # past value
                ;;
        -p | --password)
                PASSWORD="$2"
                shift # past argument
                shift # past value
                ;;
        -s | --autostart_station)
                AUTOSTART_STATION="autostart_station = $2"
                shift # past argument
                shift # past value
                ;;
        --default | -h | --help)
                DEFAULT=YES
                echo "-u --username --> Login username for Pandora Radio"
                echo "-p --password --> Login password for Pandora Radio"
                echo "-s --autostart_station --> The default station for Pandora to play on start"
                echo "-h --help --> Prints these options"
                shift # past argument
                exit 0
                ;;
        *) # unknown option
                POSITIONAL+=("$1") # save it in an array for later
                shift              # past argument
                ;;
        esac
done
set -- "${POSITIONAL[@]}" # restore positional parameters

sudo apt update
sudo apt install -y \
        pianobar \
        npm \
        screen

git clone https://github.com/kylejohnson/Patiobar.git $PATIOBAR_DIR

# Don't run npm install if it has already ran
cd $PATIOBAR_DIR
if [ ! -d "node_modules" ]; then
        echo -n 'Running `npm install`...   '
        if npm install >/dev/null 2>&1; then
                echo "success"
        else
                echo "failure"
        fi
fi
cd $SCRIPTPATH

# Don't create fifo if it already exists
if [ ! -p "${FIFO}" ]; then
        echo -n "Creating ${FIFO} control file...   "
        if mkfifo "${FIFO}" >/dev/null 2>&1; then
                echo "success"
        else
                echo "failure"
        fi
fi

echo -n "Creating the Pianobar config at ${CONFIG_DIR}/config file...   "
if mkdir -p "${CONFIG_DIR}"; then
        if ! [ -f "${CONFIG_DIR}/config" ]; then
                if cat <<EOF >>"${CONFIG_DIR}/config"; then
user = ${USER_NAME}
password = ${PASSWORD}
${AUTOSTART_STATION}
audio_quality = high
event_command = ${EVENT_COMMAND}
fifo = ${FIFO}
tls_fingerprint = ${TLS_FINGERPRINT}
EOF
                        echo "success"
                        echo "If no options were set remember to update the Pianobar config"
                else
                        echo "failure"
                fi
        else
                echo "${CONFIG_DIR}/config already exists"
        fi
fi
