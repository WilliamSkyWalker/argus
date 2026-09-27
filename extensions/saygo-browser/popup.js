async function update(type) {
  try {
    const data = await chrome.runtime.sendMessage({type});
    document.getElementById("status").textContent =
      "Saygo " + chrome.runtime.getManifest().version + "\n" +
      (data.connected ? "Connected" : "Disconnected") + (data.error ? "\n" + data.error : "") +
      "\n\nAvailable tabs:\n" + (data.pages || []).map(p => p.title + "\n" + p.page_id).join("\n\n");
  } catch (error) { document.getElementById("status").textContent = String(error); }
}
for (const type of ["connect","release"]) document.getElementById(type).onclick = () => update(type);
void update("status");
