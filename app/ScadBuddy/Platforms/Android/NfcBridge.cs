using System.Diagnostics.Metrics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Android.App;
using Android.Nfc;
using Android.Nfc.Tech;
using AndroidX.WebKit;
using Microsoft.Extensions.Logging;
using AWebView = Android.Webkit.WebView;

namespace ScadBuddy;

/// <summary>
/// The page's NFC, on Android's own API (#7905). The page talks to it through
/// `window.ScadBuddyNfcNative` (a WebMessageListener) and the injected NDEFReader
/// (Resources/Raw/nfc-polyfill.js); both are offered only to the configured ScadBuddy origin.
/// A MIFARE Classic tag is read with the Bambu keys, derived from its UID
/// (https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/main/deriveKeys.py).
/// A device call (<see cref="DeviceMethods"/>) takes the taps while it runs (<see cref="StartCall"/>).
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

	public static void Attach(AWebView webView, IEnumerable<string> origins)
	{
		var rules = origins.ToList();
		if (!WebViewFeature.IsFeatureSupported(WebViewFeature.WebMessageListener) ||
			!WebViewFeature.IsFeatureSupported(WebViewFeature.DocumentStartScript))
			throw new InvalidOperationException("This phone's Android System WebView is too old for ScadBuddy's NFC. Update it from the Play Store.");
		WebViewCompat.AddWebMessageListener(webView, "ScadBuddyNfcNative", rules, Instance);
		using var stream = FileSystem.OpenAppPackageFileAsync("nfc-polyfill.js").GetAwaiter().GetResult();
		using var text = new StreamReader(stream);
		WebViewCompat.AddDocumentStartJavaScript(webView, text.ReadToEnd(), rules);
	}

	// The running device call's taps (DeviceMethods): while one is set, every tap goes to it.
	Action<Tag>? callTaps;

	/// <summary>Sends every tap to <paramref name="taps"/> until <see cref="EndCall"/>: the page's scan and write get none meanwhile.</summary>
	public void StartCall(Action<Tag> taps)
	{
		lock (gate) callTaps = taps;
	}

	public void EndCall(Action<Tag> taps)
	{
		lock (gate) if (callTaps == taps) callTaps = null;
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
		// Android's presence check re-selects the tag every 125 ms by default, which drops a
		// MIFARE Classic sector's authentication in the middle of a read: wait 5 s instead.
		var extras = new Android.OS.Bundle();
		extras.PutInt(NfcAdapter.ExtraReaderPresenceCheckDelay, 5000);
		adapter.EnableReaderMode(a, this, flags, extras);
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
				case "close":
					lock (gate) scanning = false;
					Reply(id, true);
					MainThread.BeginInvokeOnMainThread(() => activity?.MoveTaskToBack(true));
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
		Action<Tag>? call;
		lock (gate) { lastTag = tag; pending = write; reading = scanning; call = callTaps; }
		if (call is not null)
		{
			call(tag);
			return;
		}
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
			WriteNdef(tag, pending.Message);
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

	/// <summary>
	/// Writes an NDEF message to the tag. A refusal (a MIFARE Classic tag, a read-only tag, a tag
	/// with no NDEF) is a <see cref="BridgeError"/>; any other exception is a failed transfer.
	/// </summary>
	public static void WriteNdef(Tag tag, NdefMessage message)
	{
		// A Bambu spool's tag is MIFARE Classic, and a Bambu tag is never written: refuse every
		// MIFARE Classic tag, before NdefFormatable could format it.
		if (IsClassic(tag))
			throw new BridgeError("NotAllowedError", "This is a MIFARE Classic tag, such as a Bambu spool's. ScadBuddy never writes one.");
		var ndef = Ndef.Get(tag);
		if (ndef is not null)
		{
			try
			{
				ndef.Connect();
				if (!ndef.IsWritable) throw new BridgeError("NotAllowedError", "This tag is read-only.");
				ndef.WriteNdefMessage(message);
			}
			finally { Close(ndef); }
			return;
		}
		var formatable = NdefFormatable.Get(tag) ??
			throw new BridgeError("NotSupportedError", "This tag cannot hold NDEF records.");
		try
		{
			formatable.Connect();
			formatable.Format(message);
		}
		finally { Close(formatable); }
	}

	public static bool IsClassic(Tag tag) => tag.GetTechList()?.Contains("android.nfc.tech.MifareClassic") == true;

	public static bool HasNdef(Tag tag) => tag.GetTechList()?.Contains("android.nfc.tech.Ndef") == true;

	/// <summary>The tag's tech, as a device read names it: mifare_classic, ndef, ndef_formatable, or Android's first tech.</summary>
	public static string TechOf(Tag tag)
	{
		var techs = tag.GetTechList() ?? [];
		if (techs.Contains("android.nfc.tech.MifareClassic")) return "mifare_classic";
		if (techs.Contains("android.nfc.tech.Ndef")) return "ndef";
		if (techs.Contains("android.nfc.tech.NdefFormatable")) return "ndef_formatable";
		return techs.FirstOrDefault()?.Split('.').Last().ToLowerInvariant() ?? "unknown";
	}

	/// <summary>
	/// A device call's read: the page's reading (<c>serialNumber</c>, <c>records</c>, and
	/// <c>mifare</c> for a MIFARE Classic tag), with its progress told to <paramref name="progress"/>.
	/// </summary>
	public static JsonObject ReadTag(Tag tag, ITagProgress progress) => Read(tag, progress);

	// Reads by kind (mifare_classic or ndef) and outcome: complete, partial (a sector refused,
	// or the read stopped and the next tap resumes it) or error.
	static readonly Counter<long> Reads = Telemetry.Meter.CreateCounter<long>("scadbuddy_app.nfc.reads");
	static readonly Histogram<double> ReadDuration = Telemetry.Meter.CreateHistogram<double>("scadbuddy_app.nfc.read.duration", "s");
	// MIFARE reads that started from the sectors an earlier, partial read of the same tag kept.
	static readonly Counter<long> ResumedReads = Telemetry.Meter.CreateCounter<long>("scadbuddy_app.nfc.resumed_reads");

	static JsonObject Read(Tag tag, ITagProgress? progress = null)
	{
		using var span = Telemetry.Source.StartActivity("nfc.read");
		var started = System.Diagnostics.Stopwatch.GetTimestamp();
		var ndefFailed = false;
		var resumed = false;
		var uid = tag.GetId() ?? [];
		var techs = tag.GetTechList() ?? [];
		var isClassic = techs.Contains("android.nfc.tech.MifareClassic");
		var serial = string.Join(":", uid.Select(b => b.ToString("x2")));
		if (isClassic && uid.Length == 4) serial += AmsUidSuffix;
		var records = new JsonArray();
		JsonObject? mifare = null;
		Ndef? ndef = null;
		try
		{
			ndef = Ndef.Get(tag);
			if (ndef is not null)
			{
				ndef.Connect();
				var message = ndef.NdefMessage ?? ndef.CachedNdefMessage;
				foreach (var record in message?.GetRecords() ?? []) records.Add(NdefWire.Parse(record));
			}
		}
		catch (Exception error)
		{
			ndefFailed = true;
			if (!isClassic) progress?.Lost(0, $"ndef: {error.GetType().Name}: {error.Message}");
		}
		// A Tag has one connected technology at a time: close Ndef before MifareClassic connects,
		// and before a write or the next read connects again.
		finally { Close(ndef); }
		if (!isClassic && !ndefFailed) progress?.Found(Convert.ToHexString(uid), TechOf(tag), 0, 0);
		if (isClassic) mifare = ReadClassic(tag, uid, out resumed, progress);
		Android.Util.Log.Info("ScadBuddyNfc",
			$"read {Convert.ToHexString(uid)} techs={string.Join(',', techs.Select(t => t.Split('.').Last()))} records={records.Count} " +
			(mifare is null ? "mifare=none" : $"blocks={mifare["blocks"]!.AsArray().Count} refused={mifare["refusedSectors"]!.ToJsonString()} error={mifare["error"]?.ToString() ?? "none"}"));
		Record(span, started, techs, records.Count, isClassic, ndefFailed, resumed, mifare);
		// A device call's read shows its progress on the call's screen instead.
		if (mifare is not null && progress is null)
		{
			var count = mifare["blocks"]!.AsArray().Count;
			// Sectors 0-4 (blocks 1-18) hold every field that names the filament; the rest is
			// the signature, so a read that has them already identifies the spool.
			var identified = Enumerable.Range(0, 5).All(s => mifare["blocks"]!.AsArray().Any(b => b!["index"]!.GetValue<int>() == s * 4));
			Toast(mifare["error"] is null
				? $"Tag read: {count} blocks."
				: identified
					? $"Spool identified ({count} of 48 blocks). Tap again to finish the full read."
					: $"Read {count} blocks. Hold the tag still and tap again to finish.");
		}
		var reading = new JsonObject { ["type"] = "reading", ["serialNumber"] = serial, ["records"] = records };
		if (mifare is not null) reading["mifare"] = mifare;
		return reading;
	}

	/// <summary>The read's span attributes, metrics and OTLP log: counts and names only, never the UID or block data.</summary>
	static void Record(System.Diagnostics.Activity? span, long started, IList<string> techs, int records, bool isClassic, bool ndefFailed, bool resumed, JsonObject? mifare)
	{
		var kind = isClassic ? "mifare_classic" : "ndef";
		var blocks = mifare?["blocks"]!.AsArray().Count ?? 0;
		var refused = mifare?["refusedSectors"]!.AsArray().Count ?? 0;
		// The error's kind (its sector and exception type), never Android's message.
		var error = mifare?["error"]?.ToString() is { } failure ? string.Join(": ", failure.Split(": ").Take(2)) : null;
		var outcome = isClassic
			? mifare is null ? "error" : error is not null || refused > 0 ? "partial" : "complete"
			: ndefFailed ? "error" : "complete";
		var shortTechs = string.Join(',', techs.Select(t => t.Split('.').Last()));
		span?.SetTag("nfc.techs", shortTechs);
		span?.SetTag("nfc.kind", kind);
		span?.SetTag("nfc.ndef.records", records);
		span?.SetTag("nfc.outcome", outcome);
		if (isClassic)
		{
			span?.SetTag("nfc.mifare.blocks", blocks);
			span?.SetTag("nfc.mifare.refused_sectors", refused);
			span?.SetTag("nfc.mifare.resumed", resumed);
		}
		if (outcome == "error" || error is not null)
			span?.SetStatus(System.Diagnostics.ActivityStatusCode.Error, error ?? "read failed");
		var tags = new System.Diagnostics.TagList { { "kind", kind }, { "outcome", outcome } };
		Reads.Add(1, tags);
		ReadDuration.Record(System.Diagnostics.Stopwatch.GetElapsedTime(started).TotalSeconds, tags);
		if (resumed) ResumedReads.Add(1);
		Telemetry.Nfc.LogInformation(
			"nfc read {Kind} {Outcome} techs={Techs} records={Records} blocks={Blocks} refused={Refused} resumed={Resumed} error={Error}",
			kind, outcome, shortTechs, records, blocks, refused, resumed, error ?? "none");
	}

	static JsonObject? ReadClassic(Tag tag, byte[] uid, out bool resumed, ITagProgress? progress)
	{
		resumed = false;
		MifareClassic? classic;
		try { classic = MifareClassic.Get(tag); } catch (Exception) { return null; }
		if (classic is null) return null;
		var keys = HKDF.DeriveKey(HashAlgorithmName.SHA256, uid, 16 * 6, Salt, Info);
		var hex = Convert.ToHexString(uid);
		// A failed transfer halts the card until Android discovers it again, so a read that
		// stops partway keeps its sectors and the next tap of the same tag reads only the rest.
		Dictionary<int, JsonArray> sectors;
		lock (PartialGate)
		{
			// One partial read per UID: a Bambu spool has two tags, and a tap on one side must not
			// drop what the other side's last tap read.
			foreach (var old in Partials.Where(e => DateTime.UtcNow - e.Value.At >= PartialFor).Select(e => e.Key).ToList())
				Partials.Remove(old);
			sectors = Partials.TryGetValue(hex, out var p) ? p.Sectors : [];
		}
		resumed = sectors.Count > 0;
		var refused = new JsonArray();
		var at = -1;
		string? failure = null;
		var of = DeviceProtocol.DataBlocks(classic.BlockCount);
		int Have() => sectors.Values.Sum(s => s.Count);
		{
			try
			{
				classic.Timeout = 1000;
				classic.Connect();
				progress?.Found(hex, "mifare_classic", of, Have());
				for (var sector = 0; sector < classic.SectorCount; sector++)
				{
					at = sector;
					if (sectors.ContainsKey(sector)) continue;
					// The call's deadline is near: stop with what was read, which the dump keeps.
					if (progress?.Stopped == true) throw new OperationCanceledException("the call stopped the read");
					var key = sector < 16 ? keys.AsSpan(sector * 6, 6).ToArray() : null;
					var read = key is null ? null : ReadSector(classic, sector, key);
					if (read is null) refused.Add(sector); else sectors[sector] = read;
					progress?.Read(Have(), of);
				}
			}
			catch (Exception error)
			{
				failure = $"sector {at}: {error.GetType().Name}: {error.Message}";
				progress?.Lost(Have(), failure);
			}
			finally { Close(classic); }
		}
		lock (PartialGate)
			if (failure is null) Partials.Remove(hex); else Partials[hex] = (sectors, DateTime.UtcNow);
		var blocks = new JsonArray();
		foreach (var sector in sectors.Keys.Order())
			foreach (var block in sectors[sector]) blocks.Add(block!.DeepClone());
		var result = new JsonObject { ["uid"] = hex, ["blocks"] = blocks, ["refusedSectors"] = refused };
		if (failure is not null) result["error"] = failure;
		return result;
	}

	/// <summary>
	/// Closes a tag technology. Dispose releases only the managed peer, not the connection, and a
	/// Tag refuses a second connect ("Close other technology first!") until close() runs.
	/// </summary>
	static void Close(Android.Nfc.Tech.ITagTechnology? tech)
	{
		if (tech is null) return;
		try { tech.Close(); } catch (Java.IO.IOException) { }
		(tech as IDisposable)?.Dispose();
	}

	static readonly object PartialGate = new();
	static readonly TimeSpan PartialFor = TimeSpan.FromMinutes(2);
	static readonly Dictionary<string, (Dictionary<int, JsonArray> Sectors, DateTime At)> Partials = [];

	/// <summary>A sector's data blocks, or null when the key is refused. The last block of a sector is its key trailer: never read.</summary>
	static JsonArray? ReadSector(MifareClassic classic, int sector, byte[] key)
	{
		if (!classic.AuthenticateSectorWithKeyA(sector, key)) return null;
		var first = classic.SectorToBlock(sector);
		var count = classic.GetBlockCountInSector(sector);
		var read = new JsonArray();
		for (var at = 0; at < count - 1; at++)
		{
			var data = classic.ReadBlock(first + at);
			read.Add(new JsonObject { ["index"] = first + at, ["dataHex"] = Convert.ToHexString(data).ToLowerInvariant() });
		}
		return read;
	}

	public static void Notify(string text) => Toast(text);

	static void Toast(string text) =>
		MainThread.BeginInvokeOnMainThread(() =>
			Android.Widget.Toast.MakeText(Android.App.Application.Context, text, Android.Widget.ToastLength.Short)!.Show());

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

	internal sealed class BridgeError(string name, string message) : Exception(message)
	{
		public string Name { get; } = name;
	}
}

/// <summary>What a device call's read tells as it goes (tag_found, tag_progress, tag_lost). The UID is hex; telemetry never gets it.</summary>
public interface ITagProgress
{
	void Found(string uid, string tech, int blocks, int resumeFrom);
	void Read(int read, int of);
	void Lost(int read, string error);

	/// <summary>Whether the call wants the read to stop before its next sector.</summary>
	bool Stopped { get; }
}
