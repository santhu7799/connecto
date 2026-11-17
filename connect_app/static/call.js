/* Direct (friend-to-friend) voice & video calls.
 * Loaded on every logged-in page: incoming calls ring wherever you are.
 * Exposes window.ConnectCall.start(peer, video) for the chat header buttons.
 */
(() => {
  if (!window.sock) return;
  const sock = window.sock;

  // ---------- overlay UI ----------
  const overlay = document.createElement("div");
  overlay.id = "call-overlay";
  overlay.hidden = true;
  overlay.innerHTML = `
    <div class="call-box">
      <div class="call-videos" hidden>
        <video id="call-remote-video" autoplay playsinline></video>
        <video id="call-local-video" autoplay playsinline muted></video>
      </div>
      <div class="call-id">
        <span class="avatar avatar-xl" id="call-avatar"></span>
        <h2 id="call-name"></h2>
        <div class="muted" id="call-status"></div>
        <div class="rc-timer" id="call-dc-timer" hidden>00:00</div>
      </div>
      <div class="rc-controls" id="call-actions"></div>
    </div>`;
  const toast = document.createElement("div");
  toast.id = "call-toast";
  toast.hidden = true;

  document.addEventListener("DOMContentLoaded", () => {
    document.body.append(overlay, toast);
  });
  if (document.readyState !== "loading") document.body.append(overlay, toast);

  const $ = (id) => overlay.querySelector("#" + id);
  const remoteAudio = new Audio();
  remoteAudio.autoplay = true;

  // ---------- state ----------
  let state = "idle";            // idle | out (calling) | in (ringing) | active
  let inviteId = null;           // incoming invite being shown
  let peer = null, isVideo = false;
  let pc = null, localStream = null, pendingCandidates = [];
  let muted = false, camOff = false;
  let timerHandle = null, seconds = 0, ringTimeout = null;

  function showToast(msg) {
    toast.textContent = msg;
    toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toast.hidden = true; }, 4000);
  }

  function setAvatar(p) {
    const av = $("call-avatar");
    av.style.setProperty("--h", p.hue);
    av.innerHTML = "";
    if (p.photo) {
      const img = document.createElement("img");
      img.alt = "";
      img.src = p.photo;
      av.append(img);
    } else {
      av.textContent = p.initial;
    }
  }

  function button(label, icon, cls, onClick) {
    const b = document.createElement("button");
    b.className = "rc-ctl " + (cls || "");
    b.innerHTML = icon + "<small></small>";
    b.querySelector("small").textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function render() {
    overlay.hidden = state === "idle";
    if (state === "idle") return;
    setAvatar(peer);
    $("call-name").textContent = peer.name + (peer.age ? ", " + peer.age : "");
    const actions = $("call-actions");
    actions.innerHTML = "";
    $("call-dc-timer").hidden = state !== "active";
    overlay.querySelector(".call-videos").hidden = !(state === "active" && isVideo);
    overlay.querySelector(".call-id").classList.toggle("compact", state === "active" && isVideo);

    if (state === "out") {
      $("call-status").textContent = (isVideo ? "Video calling…" : "Calling…");
      actions.append(button("Cancel", "📞", "rc-end", cancelOutgoing));
    } else if (state === "in") {
      $("call-status").textContent = "Incoming " + (isVideo ? "video" : "voice") + " call";
      actions.append(
        button("Accept", isVideo ? "🎥" : "📞", "call-accept", accept),
        button("Decline", "📞", "rc-end", decline));
    } else if (state === "active") {
      $("call-status").textContent = "Connecting…";
      const muteBtn = button(muted ? "Unmute" : "Mute", "🎤", muted ? "active" : "", () => {
        muted = !muted;
        if (localStream) localStream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
        render();
      });
      actions.append(muteBtn);
      if (isVideo) {
        actions.append(button(camOff ? "Cam on" : "Cam off", "🎥", camOff ? "active" : "", () => {
          camOff = !camOff;
          if (localStream) localStream.getVideoTracks().forEach((t) => { t.enabled = !camOff; });
          render();
        }));
      }
      actions.append(button("End", "📞", "rc-end", () => { sock.emit("dc_hangup"); teardown("Call ended."); }));
    }
  }

  function startTimer() {
    seconds = 0;
    timerHandle = setInterval(() => {
      seconds++;
      const m = String(Math.floor(seconds / 60)).padStart(2, "0");
      const s = String(seconds % 60).padStart(2, "0");
      $("call-dc-timer").textContent = m + ":" + s;
    }, 1000);
  }

  function teardown(message) {
    clearTimeout(ringTimeout); ringTimeout = null;
    clearInterval(timerHandle); timerHandle = null;
    pendingCandidates = [];
    if (pc) { pc.onicecandidate = pc.ontrack = pc.onconnectionstatechange = null; pc.close(); pc = null; }
    if (localStream) { localStream.getTracks().forEach((t) => t.stop()); localStream = null; }
    remoteAudio.srcObject = null;
    $("call-remote-video").srcObject = null;
    $("call-local-video").srcObject = null;
    $("call-dc-timer").textContent = "00:00";
    state = "idle"; inviteId = null; peer = null; muted = false; camOff = false;
    render();
    if (message) showToast(message);
  }

  async function getMedia() {
    localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: isVideo });
    return localStream;
  }

  async function setupPeerConnection(initiator) {
    pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    localStream.getTracks().forEach((t) => pc.addTrack(t, localStream));
    if (isVideo) $("call-local-video").srcObject = localStream;
    pc.onicecandidate = (e) => { if (e.candidate) sock.emit("dc_signal", { candidate: e.candidate }); };
    pc.ontrack = (e) => {
      if (isVideo) $("call-remote-video").srcObject = e.streams[0];
      else remoteAudio.srcObject = e.streams[0];
    };
    pc.onconnectionstatechange = () => {
      if (!pc || state !== "active") return;
      if (pc.connectionState === "connected") {
        $("call-status").textContent = isVideo ? "Video call" : "Voice call";
        if (!timerHandle) startTimer();
      } else if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
        $("call-status").textContent = "Connection lost";
      }
    };
    if (initiator) {
      await pc.setLocalDescription(await pc.createOffer());
      sock.emit("dc_signal", { sdp: pc.localDescription });
    }
  }

  // ---------- user actions ----------
  async function start(p, video) {
    if (state !== "idle") { showToast("You're already in a call."); return; }
    peer = p; isVideo = video;
    try {
      await getMedia();
    } catch (e) {
      peer = null;
      showToast(video ? "Camera & microphone access is required for video calls."
                      : "Microphone access is required for voice calls.");
      return;
    }
    state = "out";
    render();
    sock.emit("dc_invite", { to: p.id, video: video });
    ringTimeout = setTimeout(() => { sock.emit("dc_cancel"); teardown("No answer."); }, 30000);
  }

  function cancelOutgoing() {
    sock.emit("dc_cancel");
    teardown();
  }

  async function accept() {
    const iid = inviteId;
    try {
      await getMedia();
    } catch (e) {
      sock.emit("dc_decline", { invite: iid });
      teardown(isVideo ? "Camera & microphone access is required for video calls."
                       : "Microphone access is required for voice calls.");
      return;
    }
    sock.emit("dc_accept", { invite: iid });
  }

  function decline() {
    sock.emit("dc_decline", { invite: inviteId });
    teardown();
  }

  // ---------- socket events ----------
  sock.on("dc_ringing", () => { if (state === "out") $("call-status").textContent = "Ringing…"; });

  sock.on("dc_incoming", (d) => {
    if (state !== "idle") {            // already busy here -> auto-decline
      sock.emit("dc_decline", { invite: d.invite });
      return;
    }
    inviteId = d.invite;
    peer = d.caller;
    isVideo = d.video;
    state = "in";
    render();
  });

  sock.on("dc_dismiss", (d) => {
    if (state === "in" && d.invite === inviteId) teardown("Call ended before you answered.");
  });

  sock.on("dc_accepted", async (d) => {
    clearTimeout(ringTimeout); ringTimeout = null;
    peer = d.peer; isVideo = d.video;
    state = "active";
    render();
    try {
      if (!localStream) await getMedia();
      await setupPeerConnection(d.initiator);
    } catch (e) {
      sock.emit("dc_hangup");
      teardown("Couldn't start the call: " + e.message);
    }
  });

  sock.on("dc_signal", async (d) => {
    if (!pc) return;
    try {
      if (d.sdp) {
        await pc.setRemoteDescription(d.sdp);
        for (const c of pendingCandidates) await pc.addIceCandidate(c);
        pendingCandidates = [];
        if (d.sdp.type === "offer") {
          await pc.setLocalDescription(await pc.createAnswer());
          sock.emit("dc_signal", { sdp: pc.localDescription });
        }
      } else if (d.candidate) {
        if (pc.remoteDescription) await pc.addIceCandidate(d.candidate);
        else pendingCandidates.push(d.candidate);
      }
    } catch (e) { /* ignore stale signals */ }
  });

  sock.on("dc_declined", () => teardown((peer ? peer.name : "They") + " declined the call."));
  sock.on("dc_failed", (d) => teardown(d.message));
  sock.on("dc_ended", () => teardown("Call ended."));
  sock.on("disconnect", () => { if (state !== "idle") teardown("Connection to the server was lost."); });

  window.addEventListener("beforeunload", () => {
    if (state === "active") sock.emit("dc_hangup");
    else if (state === "out") sock.emit("dc_cancel");
    else if (state === "in") sock.emit("dc_decline", { invite: inviteId });
  });

  window.ConnectCall = { start };
})();
