using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Android.App;
using Android.Nfc;
using Android.Nfc.Tech;
using AndroidX.WebKit;
using AWebView = Android.Webkit.WebView;

namespace ScadBuddy;

/// <summary>
/// The page's NFC, on Android's own API (#7905). The page talks to it through
/// `window.ScadBuddyNfcNative` (a WebMessageListener) and the injected NDEFReader
/// (Resources/Raw/nfc-polyfill.js); both are offered only to the configured ScadBuddy origin.
/// A MIFARE Classic tag is read with the Bambu keys, derived from its UID
/// (https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/main/deriveKeys.py).
/// </summary>
public sealed class NfcBridge : Java.Lang.Object, NfcAdapter.IReaderCallback, WebViewCompat.IWebMessageListener
{
	public static NfcBridge Instance { get; } = new();

	// deriveKeys.py: HKDF-SHA256 over the UID, this salt, info "RFID-A\0", 16 keys of 6 bytes.
	static readonly byte[] Salt = Convert.FromHexString("9A759CF2C4F7CAFF222CB9769B41BC96");
	static readonly byte[] Info = Encoding.ASCII.GetBytes("RFID-A\0");
	// The AMS reports a 4-byte UID followed by these bytes (spec 2026-11-26, measured on prod).
	const string AmsUidSuffix = ":00:00:01:00";

	readonly object gate = new();
	JavaScriptReplyProxy? reply;
	bool scanning;
	PendingWrite? write;
	Tag? lastTag;
	Activity? activity;

	sealed record PendingWrite(long JsId, long WriteId, NdefMessage Message);

	public static void Attach(AWebView webView, string origin)
	{
		var rules = new List<string> { origin };
		if (!WebViewFeature.IsFeatureSupported(WebViewFeature.WebMessageListener) ||
			!WebViewFeature.IsFeatureSupported(WebViewFeature.DocumentStartScript))
			throw new InvalidOperationException("This phone's Android System WebView is too old for ScadBuddy's NFC. Update it from the Play Store.");
		WebViewCompat.AddWebMessageListener(webView, "ScadBuddyNfcNative", rules, Instance);
		using var stream = FileSystem.OpenAppPackageFileAsync("nfc-polyfill.js").GetAwaiter().GetResult();
		using var text = new StreamReader(stream);
		WebViewCompat.AddDocumentStartJavaScript(webView, text.ReadToEnd(), rules);
	}

	public void Resume(Activity resumed)
	{
		activity = resumed;
		UpdateReaderMode();
	}

	public void Pause(Activity paused)
	{
		NfcAdapter.GetDefaultAdapter(paused)?.DisableReaderMode(paused);
		activity = null;
	}

	// Reader mode stays on while the app is in front, so a tap never opens another app; taps
	// with no scan or write waiting are dropped.
	void UpdateReaderMode()
	{
		var a = activity;
		if (a is null) return;
		var adapter = NfcAdapter.GetDefaultAdapter(a);
		if (adapter is null) return;
		var flags = NfcReaderFlags.NfcA | NfcReaderFlags.NfcB | NfcReaderFlags.NfcF | NfcReaderFlags.NfcV;
		adapter.EnableReaderMode(a, this, flags, null);
	}

	public void OnPostMessage(AWebView view, WebMessageCompat message, Android.Net.Uri sourceOrigin, bool isMainFrame, JavaScriptReplyProxy replyProxy)
	{
		if (!isMainFrame || message.Data is null) return;
		reply = replyProxy;
		JsonNode? body;
		try { body = JsonNode.Parse(message.Data); } catch (JsonException) { return; }
		if (body is null) return;
		var id = body["id"]?.GetValue<long>() ?? 0;
		var op = body["op"]?.GetValue<string>();
		try
		{
			switch (op)
			{
				case "scan":
					if (activity is not null && NfcAdapter.GetDefaultAdapter(activity) is { IsEnabled: false })
						throw new BridgeError("NotReadableError", "NFC is off on this phone. Turn it on in Android's settings.");
					lock (gate) scanning = true;
					Reply(id, true);
					break;
				case "stopScan":
					lock (gate) scanning = false;
					Reply(id, true);
					break;
				case "write":
					var lang = body["lang"]?.GetValue<string>() ?? "en";
					var records = NdefWire.Build(body["records"]!.AsArray(), lang);
					var pending = new PendingWrite(id, body["writeId"]!.GetValue<long>(), new NdefMessage(records));
					Tag? present;
					lock (gate) { write = pending; present = lastTag; }
					// The page writes while the tag it just read is still on the phone (tagWrite.ts).
					if (present is not null) Task.Run(() => TryWrite(present, fromTap: false));
					break;
				case "cancelWrite":
					var writeId = body["writeId"]?.GetValue<long>();
					lock (gate) if (write?.WriteId == writeId) write = null;
					Reply(id, true);
					break;
				default:
					throw new BridgeError("NotSupportedError", $"Unknown operation {op}.");
			}
		}
		catch (BridgeError error) { Fail(id, error.Name, error.Message); }
		catch (Exception error) { Fail(id, "TypeError", error.Message); }
	}

	public void OnTagDiscovered(Tag? tag)
	{
		if (tag is null) return;
		PendingWrite? pending;
		bool reading;
		lock (gate) { lastTag = tag; pending = write; reading = scanning; }
		if (pending is not null) { TryWrite(tag, fromTap: true); return; }
		if (reading) Emit(Read(tag));
	}

	void TryWrite(Tag tag, bool fromTap)
	{
		PendingWrite? pending;
		lock (gate) pending = write;
		if (pending is null) return;
		try
		{
			using (var ndef = Ndef.Get(tag))
			{
				if (ndef is not null)
				{
					ndef.Connect();
					if (!ndef.IsWritable) throw new BridgeError("NotAllowedError", "This tag is read-only.");
					ndef.WriteNdefMessage(pending.Message);
				}
				else
				{
					using var formatable = NdefFormatable.Get(tag) ??
						throw new BridgeError("NotSupportedError", "This tag cannot hold NDEF records.");
					formatable.Connect();
					formatable.Format(pending.Message);
				}
			}
			lock (gate) if (write == pending) write = null; else return;
			Reply(pending.JsId, true);
			// The read-back the page waits for next (tagWrite.ts `readBack`).
			Emit(Read(tag));
		}
		catch (BridgeError error)
		{
			lock (gate) if (write == pending) write = null;
			Fail(pending.JsId, error.Name, error.Message);
		}
		catch (Exception) when (!fromTap)
		{
			// The tag left before the write: wait for the next tap.
		}
		catch (Exception error)
		{
			lock (gate) if (write == pending) write = null;
			Fail(pending.JsId, "NetworkError", $"The write failed: {error.Message}");
		}
	}

	static JsonObject Read(Tag tag)
	{
		var uid = tag.GetId() ?? [];
		var techs = tag.GetTechList() ?? [];
		var isClassic = techs.Contains("android.nfc.tech.MifareClassic");
		var serial = string.Join(":", uid.Select(b => b.ToString("x2")));
		if (isClassic && uid.Length == 4) serial += AmsUidSuffix;
		var records = new JsonArray();
		JsonObject? mifare = null;
		try
		{
			using var ndef = Ndef.Get(tag);
			if (ndef is not null)
			{
				ndef.Connect();
				var message = ndef.NdefMessage ?? ndef.CachedNdefMessage;
				foreach (var record in message?.GetRecords() ?? []) records.Add(NdefWire.Parse(record));
			}
		}
		catch (Exception) { }
		if (isClassic) mifare = ReadClassic(tag, uid);
		var reading = new JsonObject { ["type"] = "reading", ["serialNumber"] = serial, ["records"] = records };
		if (mifare is not null) reading["mifare"] = mifare;
		return reading;
	}

	static JsonObject? ReadClassic(Tag tag, byte[] uid)
	{
		MifareClassic? classic;
		try { classic = MifareClassic.Get(tag); } catch (Exception) { return null; }
		if (classic is null) return null;
		var keys = HKDF.DeriveKey(HashAlgorithmName.SHA256, uid, 16 * 6, Salt, Info);
		var blocks = new JsonArray();
		var refused = new JsonArray();
		using (classic)
		{
			try
			{
				classic.Connect();
				for (var sector = 0; sector < classic.SectorCount; sector++)
				{
					var key = sector < 16 ? keys.AsSpan(sector * 6, 6).ToArray() : null;
					if (key is null || !classic.AuthenticateSectorWithKeyA(sector, key)) { refused.Add(sector); continue; }
					var first = classic.SectorToBlock(sector);
					var count = classic.GetBlockCountInSector(sector);
					// The last block of a sector is its key trailer: never sent.
					for (var at = 0; at < count - 1; at++)
					{
						var data = classic.ReadBlock(first + at);
						blocks.Add(new JsonObject { ["index"] = first + at, ["dataHex"] = Convert.ToHexString(data).ToLowerInvariant() });
					}
				}
			}
			catch (Exception error)
			{
				return new JsonObject { ["uid"] = Convert.ToHexString(uid), ["blocks"] = blocks, ["refusedSectors"] = refused, ["error"] = error.Message };
			}
		}
		return new JsonObject { ["uid"] = Convert.ToHexString(uid), ["blocks"] = blocks, ["refusedSectors"] = refused };
	}

	void Emit(JsonObject message) => Send(message.ToJsonString());

	void Reply(long id, bool value) =>
		Send(new JsonObject { ["type"] = "reply", ["id"] = id, ["value"] = value }.ToJsonString());

	void Fail(long id, string name, string message) =>
		Send(new JsonObject { ["type"] = "reply", ["id"] = id, ["error"] = new JsonObject { ["name"] = name, ["message"] = message } }.ToJsonString());

	void Send(string json)
	{
		var proxy = reply;
		if (proxy is null) return;
		MainThread.BeginInvokeOnMainThread(() => proxy.PostMessage(json));
	}

	sealed class BridgeError(string name, string message) : Exception(message)
	{
		public string Name { get; } = name;
	}
}
