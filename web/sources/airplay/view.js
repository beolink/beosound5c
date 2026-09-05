/**
 * AirPlay Source Preset
 *
 * AirPlay is a push source: there is nothing here to browse, because the
 * sender owns the queue. So instead of an arc browser this view is a status
 * panel — it says whether the BeoSound is waiting for a device or currently
 * receiving a stream, and from whom.
 *
 * Transport keys are deliberately NOT handled here. The router forwards them
 * to beo-source-airplay, which proxies them to the sender over DACP; routing
 * them through the UI as well would double-send.
 *
 * Now-playing rendering is left to DEFAULT_PLAYING_PRESET, which already does
 * exactly the right thing with the metadata this source pushes.
 */

const _airplayController = (() => {
    const STATUS_URL = () =>
        `${window.AppConfig?.airplayServiceUrl || 'http://localhost:8775'}/status`;
    const POLL_INTERVAL = 3000;

    let pollTimer = null;
    let lastRender = null;   // dedupe gate, avoids pointless DOM writes

    function esc(s) {
        return String(s ?? '').replace(/[&<>"]/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
        ));
    }

    function render(status) {
        const el = document.getElementById('airplay-status');
        if (!el) return;

        let html;
        if (!status) {
            html = `<div class="airplay-headline">AirPlay unavailable</div>
                    <div class="airplay-sub">The AirPlay service is not responding</div>`;
        } else if (status.play_state === 'idle') {
            // Explicit break: left to wrap on its own this strands "Mac"
            // alone on the second line at the display's aspect ratio.
            html = `<div class="airplay-headline">Waiting for a device</div>
                    <div class="airplay-sub">Choose this BeoSound from AirPlay<br>
                        on your iPhone, iPad or Mac</div>`;
        } else {
            const title = esc(status.title) || 'Streaming';
            const artist = esc(status.artist);
            const from = status.sender
                ? `Streaming from ${esc(status.sender)}`
                : 'Streaming over AirPlay';
            html = `<div class="airplay-headline">${title}</div>
                    ${artist ? `<div class="airplay-sub">${artist}</div>` : ''}
                    <div class="airplay-from">${from}</div>`;
        }

        if (html === lastRender) return;
        lastRender = html;
        el.innerHTML = html;
    }

    async function refresh() {
        try {
            const resp = await fetch(STATUS_URL());
            render(resp.ok ? await resp.json() : null);
        } catch {
            render(null);
        }
    }

    return {
        get isActive() { return true; },
        updateMetadata() {},

        start() {
            refresh();
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = setInterval(refresh, POLL_INTERVAL);
        },

        stop() {
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = null;
            lastRender = null;
        },
    };
})();

window.SourcePresets = window.SourcePresets || {};
window.SourcePresets.airplay = {
    controller: _airplayController,
    item: { title: 'AIRPLAY', path: 'menu/airplay' },
    after: 'menu/playing',
    view: {
        title: 'AIRPLAY',
        content: '<div id="airplay-status" class="airplay-status"></div>',
    },

    onAdd() {},

    onMount() {
        _airplayController.start();
    },

    onRemove() {
        _airplayController.stop();
        const el = document.getElementById('airplay-status');
        if (el) el.innerHTML = '';
    },

    // No playing sub-preset — DEFAULT_PLAYING_PRESET renders the metadata
    // beo-source-airplay pushes (title, artist, album, cover art) correctly.
};
