FROM debian:bookworm-slim

LABEL mcp-virtual-computer.image-version="4"

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    NO_AT_BRIDGE=0 \
    GTK_MODULES=atk-bridge

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        at-spi2-core \
        ca-certificates \
        dbus-x11 \
        fonts-dejavu-core \
        gir1.2-atspi-2.0 \
        iproute2 \
        iptables \
        mousepad \
        libasound2-plugins \
        pulseaudio \
        pulseaudio-utils \
        python3-dogtail \
        python3-gi \
        sudo \
        thunar \
        wmctrl \
        x11-utils \
        x11vnc \
        xdotool \
        xfce4-panel \
        xfce4-session \
        xfce4-terminal \
        xfdesktop4 \
        xfwm4 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash computer \
    && printf '%s\n' 'computer ALL=(ALL:ALL) NOPASSWD: ALL' \
        > /etc/sudoers.d/computer \
    && chmod 0440 /etc/sudoers.d/computer \
    && visudo -cf /etc/sudoers.d/computer \
    && install -d -o computer -g computer /workspace \
    && install -d -o computer -g computer /home/computer/.config \
    && install -d -o computer -g computer /home/computer/.cache \
    && install -d -o computer -g computer /home/computer/.local \
    && install -d -o computer -g computer /home/computer/.local/bin \
    && install -d -o computer -g computer /home/computer/.local/share \
    && printf '%s\n' \
        '[Default Applications]' \
        'text/plain=mousepad.desktop' \
        > /home/computer/.config/mimeapps.list \
    && chown computer:computer /home/computer/.config/mimeapps.list

COPY start-desktop.sh /usr/local/bin/start-desktop
COPY visual-action.py /usr/local/bin/visual-action
COPY desktop-control.py /usr/local/bin/desktop-control
COPY wsproxy.py /usr/local/bin/wsproxy
RUN chmod 0755 \
    /usr/local/bin/start-desktop \
    /usr/local/bin/visual-action \
    /usr/local/bin/desktop-control \
    /usr/local/bin/wsproxy

WORKDIR /workspace
EXPOSE 6080
CMD ["/usr/local/bin/start-desktop"]
