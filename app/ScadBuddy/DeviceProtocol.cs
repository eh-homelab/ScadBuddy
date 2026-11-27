using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace ScadBuddy;

/// <summary>
/// The pure part of the device link (#8244): the claim, the realtime socket's frames, a call's
/// events and result, and the tag dump, as core's devices area takes them
/// (tools/go/core/internal/devices, internal/realtime). It uses no MAUI or Android type, so
/// ScadBuddy.Tests compiles this file on its own (net10.0).
/// </summary>
public static class DeviceProtocol
{
	/// <summary>The four methods the app announces. ScadBuddy is not a device platform: there are no others.</summary>
	public static readonly string[] Methods = ["camera.capture", "code.scan", "nfc.read", "nfc.write"];

	/// <summary>Core takes at most this many events in one POST.</summary>
	public const int EventsPerPost = 20;

	/// <summary>Core takes an error of at most this many characters.</summary>
	public const int ErrorMax = 200;

	/// <summary>The realtime socket's path on the device's origin.</summary>
	public const string SocketPath = "/api/v1/ws";

	/// <summary>The blob kinds: a tag read and a photo.</summary>
	public const string TagDump = "tag_dump";
	public const string ScanImage = "scan_image";

	/// <summary>Core's limit for a scan_image upload (blobstore.Kinds).</summary>
	public const long PhotoMax = 12 << 20;

	public const string Pong = """{"type":"pong"}""";

	static readonly Regex Uuid = new("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", RegexOptions.CultureInvariant);
	static readonly Regex Token = new("^sbdev_[A-Za-z0-9_-]{43}$", RegexOptions.CultureInvariant);

	/// <summary>The claim's body: what the app is and the methods it answers.</summary>
	public static JsonObject ClaimBody(string name, string model, string appVersion) => new()
	{
		["name"] = Cut(string.IsNullOrWhiteSpace(name) ? "Phone" : name.Trim(), 100),
		["model"] = Cut(model.Trim(), 100),
		["app_version"] = Cut(appVersion.Trim(), 50),
		["capabilities"] = new JsonArray(Methods.Select(m => (JsonNode)m).ToArray()),
	};

	/// <summary>The pairing a claim's answer gives, or null when the answer is not one.</summary>
	public static Pairing? PairingOf(string answer, Uri claim)
	{
		try
		{
			var o = JsonNode.Parse(answer) as JsonObject;
			var id = o?["device_id"]?.GetValue<string>();
			var token = o?["token"]?.GetValue<string>();
			var cap = o?["hard_cap_at"]?.GetValue<string>();
			if (id is null || !Uuid.IsMatch(id) || token is null || !Token.IsMatch(token) || !TryTime(cap, out var hardCap)) return null;
			return new Pairing(DeepLink.OriginOf(claim), id, token, hardCap);
		}
		catch (Exception error) when (error is JsonException or InvalidOperationException)
		{
			return null;
		}
	}

	/// <summary>The pairing as the app stores it (SecureStorage).</summary>
	public static string Stored(Pairing p) => new JsonObject
	{
		["origin"] = p.Origin,
		["device_id"] = p.DeviceId,
		["token"] = p.Token,
		["hard_cap_at"] = p.HardCapAt.ToString("O", CultureInfo.InvariantCulture),
	}.ToJsonString();

	/// <summary>A stored pairing, or null when the text is not one.</summary>
	public static Pairing? FromStored(string? text)
	{
		if (string.IsNullOrEmpty(text)) return null;
		try
		{
			var o = JsonNode.Parse(text) as JsonObject;
			var origin = o?["origin"]?.GetValue<string>();
			var id = o?["device_id"]?.GetValue<string>();
			var token = o?["token"]?.GetValue<string>();
			if (origin is null || !Uri.TryCreate(origin, UriKind.Absolute, out var parsed) || parsed.Scheme != "https") return null;
			if (id is null || !Uuid.IsMatch(id) || token is null || !Token.IsMatch(token) || !TryTime(o?["hard_cap_at"]?.GetValue<string>(), out var cap)) return null;
			return new Pairing(origin, id, token, cap);
		}
		catch (Exception error) when (error is JsonException or InvalidOperationException)
		{
			return null;
		}
	}

	/// <summary>The realtime socket's address on the device's origin.</summary>
	public static Uri SocketOf(string origin) => new($"wss://{new Uri(origin).Authority}{SocketPath}");

	/// <summary>The first wait before the socket dials again, and the longest.</summary>
	public static readonly TimeSpan RedialFirst = TimeSpan.FromSeconds(2), RedialMax = TimeSpan.FromSeconds(30);

	/// <summary>A socket open this long was a good connection: the backoff starts again after it.</summary>
	public static readonly TimeSpan StableFor = TimeSpan.FromMinutes(1);

	/// <summary>
	/// The wait before the socket dials again. <paramref name="last"/> is the wait before this
	/// dial (zero before the first). <paramref name="openFor"/> is how long the socket stayed
	/// open (zero when the dial failed). A server that accepts and then closes at once does
	/// not reset the backoff, so it is not dialled every 2 s.
	/// </summary>
	public static TimeSpan RedialWait(TimeSpan last, TimeSpan openFor) =>
		last <= TimeSpan.Zero || openFor >= StableFor ? RedialFirst
		: last * 2 < RedialMax ? last * 2 : RedialMax;

	/// <summary>The subscribe frame for the device's own topic, the only one core lets it follow.</summary>
	public static string Subscribe(string deviceId) =>
		new JsonObject { ["type"] = "subscribe", ["topics"] = new JsonArray("device:" + deviceId) }.ToJsonString();

	/// <summary>
	/// What a socket frame means to the device: a call for it, a ping, the end of a call (core
	/// ended it: done, or failed at its deadline), or nothing.
	/// </summary>
	public static Frame Parse(string text, string deviceId)
	{
		JsonObject? o;
		try { o = JsonNode.Parse(text) as JsonObject; }
		catch (JsonException) { return Frame.Ignored; }
		try
		{
			var type = o?["type"]?.GetValue<string>();
			if (type == "ping") return Frame.Ping;
			if (type != "event" || o!["data"] is not JsonObject data) return Frame.Ignored;
			var kind = o["kind"]?.GetValue<string>();
			var call = data["call"]?.GetValue<string>();
			if (data["device"]?.GetValue<string>() != deviceId || call is null || !Uuid.IsMatch(call)) return Frame.Ignored;
			if (kind is "device.call_done" or "device.call_failed") return new CallEnded(call);
			if (kind != "device.call_requested") return Frame.Ignored;
			var method = data["method"]?.GetValue<string>();
			if (method is null || !Methods.Contains(method)) return Frame.Ignored;
			if (data["args"] is not JsonObject args || !TryTime(data["deadline"]?.GetValue<string>(), out var deadline)) return Frame.Ignored;
			return new CallRequest(call, method, (JsonObject)args.DeepClone(), deadline);
		}
		catch (InvalidOperationException)
		{
			// A field of another JSON type than the frame's.
			return Frame.Ignored;
		}
	}

	static bool TryTime(string? text, out DateTimeOffset at) =>
		DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out at);

	/// <summary>
	/// tag_found: the tag, its tech, and its data blocks. A resumed read names in
	/// <c>resume_from</c> how many data blocks it has already, a count as <c>tag_progress.read</c>
	/// is (spec: <c>read: 8</c>, then <c>resume_from: 8</c>), not a block index.
	/// </summary>
	public static JsonObject TagFound(string uid, string tech, int blocks, int resumeFrom, string? backup = null)
	{
		var data = new JsonObject { ["uid"] = uid, ["tech"] = tech, ["blocks"] = blocks };
		if (resumeFrom > 0) data["resume_from"] = resumeFrom;
		if (backup is not null) data["backup"] = backup;
		return Event("tag_found", data);
	}

	/// <summary>tag_progress: the data blocks read so far, of all.</summary>
	public static JsonObject TagProgress(int read, int of) => Event("tag_progress", new JsonObject { ["read"] = read, ["of"] = of });

	/// <summary>tag_lost: the transfer failed after this many blocks; the next tap resumes.</summary>
	public static JsonObject TagLost(int read, string error) => Event("tag_lost", new JsonObject { ["read"] = read, ["error"] = Cut(error, ErrorMax) });

	static JsonObject Event(string kind, JsonObject data) => new() { ["kind"] = kind, ["data"] = data };

	/// <summary>The events in POST bodies of at most <see cref="EventsPerPost"/> each, in order.</summary>
	public static IEnumerable<JsonObject> EventBodies(IReadOnlyList<JsonObject> events)
	{
		for (var at = 0; at < events.Count; at += EventsPerPost)
			yield return new JsonObject { ["events"] = new JsonArray(events.Skip(at).Take(EventsPerPost).Select(e => (JsonNode)e.DeepClone()).ToArray()) };
	}

	/// <summary>
	/// Whether block <paramref name="index"/> of a MIFARE Classic tag is a sector trailer, which
	/// holds the sector's keys: every 4th block of the first 32 sectors, then every 16th.
	/// </summary>
	public static bool IsTrailer(int index) => index < 128 ? index % 4 == 3 : index % 16 == 15;

	/// <summary>The data blocks (every block but the trailers) of a tag with this many blocks.</summary>
	public static int DataBlocks(int blockCount) => Enumerable.Range(0, blockCount).Count(i => !IsTrailer(i));

	/// <summary>The data blocks of a tag with <paramref name="blockCount"/> blocks that the read does not have.</summary>
	public static List<int> MissingBlocks(int blockCount, IEnumerable<int> read)
	{
		var have = read.ToHashSet();
		return Enumerable.Range(0, blockCount).Where(i => !IsTrailer(i) && !have.Contains(i)).ToList();
	}

	/// <summary>
	/// A tag_dump blob: <c>{uid, tech, blocks: [{index, data_hex}], records}</c>. It never holds a
	/// sector trailer (core refuses a dump with one): a trailer given here is left out.
	/// </summary>
	public static string Dump(string uid, string tech, IEnumerable<(int Index, string Hex)> blocks, JsonArray records) => new JsonObject
	{
		["uid"] = uid,
		["tech"] = tech,
		["blocks"] = new JsonArray(blocks.Where(b => !IsTrailer(b.Index)).OrderBy(b => b.Index)
			.Select(b => (JsonNode)new JsonObject { ["index"] = b.Index, ["data_hex"] = b.Hex }).ToArray()),
		["records"] = records.DeepClone(),
	}.ToJsonString();

	/// <summary>An nfc.read's or an nfc.write's result: the uploaded dump and what it holds.</summary>
	public static JsonObject NfcResult(string blobKey, string uid, string tech, IReadOnlyList<int> missing) => Done(new JsonObject
	{
		["blob_key"] = blobKey,
		["uid"] = uid,
		["tech"] = tech,
		["complete"] = missing.Count == 0,
		["missing_blocks"] = new JsonArray(missing.Select(i => (JsonNode)i).ToArray()),
	});

	/// <summary>A code.scan's or a camera.capture's result: the uploaded photo.</summary>
	public static JsonObject PhotoResult(string blobKey, string mediaType) =>
		Done(new JsonObject { ["blob_key"] = blobKey, ["media_type"] = mediaType });

	static JsonObject Done(JsonObject read) => new() { ["outcome"] = "done", ["result"] = new JsonObject { ["read"] = read } };

	/// <summary>
	/// Why a read that ended (by its last tap, or at the deadline) is not sent, or null to send it.
	/// A MIFARE Classic read cut short is sent: its <c>missing_blocks</c> say it is partial. An
	/// NDEF read whose last tap failed holds no records worth sending: it is a failed call.
	/// </summary>
	public static string? UnsentRead(bool classic, string? lostError) =>
		lostError is null || classic ? null : "tag_lost: the tag moved before its records were read";

	/// <summary>The last tap an nfc.write saw before its deadline.</summary>
	public enum WriteTap { None, Classic, Lost, Written }

	/// <summary>The error of an nfc.write whose deadline came: what its last tap was says why.</summary>
	public static string WriteTimedOut(WriteTap last) => last switch
	{
		WriteTap.Written => "no_read_back: the tag was written, but not read back before the deadline",
		WriteTap.Classic => "bambu_tag: the last tag tapped was MIFARE Classic, such as a Bambu spool's; ScadBuddy never writes one",
		WriteTap.Lost => "tag_lost: the tag moved before it was written",
		_ => "no_tag: no tag was tapped before the deadline",
	};

	/// <summary>The error of a call stopped before its deadline's margin, or at it: who or what stopped it.</summary>
	public static string CallStopped(bool byPerson, bool ended) =>
		byPerson ? "cancelled: the person stopped the call on the phone"
		: ended ? "ended: the call ended before the phone was done"
		: "timeout: the call's deadline came before the phone was done";

	/// <summary>The error of an nfc.write stopped after its backup and before it wrote: nothing was written.</summary>
	public static string WriteStopped(bool byPerson, bool ended) =>
		byPerson ? "cancelled: the person stopped the write on the phone; nothing was written"
		: ended ? "ended: the call ended before the write; nothing was written"
		: "timeout: the call's deadline came before the write; nothing was written";

	/// <summary>The error of an nfc.write stopped while its read-back went up: the tag holds the new records.</summary>
	public static string WrittenNotSent(bool byPerson, bool ended) =>
		"written_not_sent: the tag was written, but its read-back was not sent: " +
		(byPerson ? "the person stopped the call on the phone" : ended ? "the call ended" : "the call's deadline came");

	/// <summary>A failed call's result. The error starts with its code, as <c>no_tag: …</c>.</summary>
	public static JsonObject Failed(string error) => new() { ["outcome"] = "error", ["error"] = Cut(error, ErrorMax) };

	/// <summary>The code of an error (its text before the first colon): what telemetry may carry.</summary>
	public static string CodeOf(string error)
	{
		var code = error.Split(':', 2)[0].Trim();
		return Regex.IsMatch(code, "^[a-z_]{1,40}$") ? code : "error";
	}

	static readonly Regex External = new("^[a-z0-9][a-z0-9.-]{0,62}:[A-Za-z0-9_.-]{1,63}$", RegexOptions.CultureInvariant);
	static readonly Regex LowerHex = new("^(?:[0-9a-f]{2})*$", RegexOptions.CultureInvariant);
	static readonly Regex Lang = new("^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8}){0,4}$", RegexOptions.CultureInvariant);
	static readonly Regex Media = new("^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$", RegexOptions.CultureInvariant);

	/// <summary>
	/// An nfc.write's records, or null and why. Core sends only the records it parsed strictly
	/// (devices/ndef.go); the app checks them again, so a record of another shape is never
	/// written: text, url, absolute-url, mime, an external type, or empty, with only their keys.
	/// </summary>
	public static JsonArray? WriteRecords(JsonObject args, out string? error)
	{
		error = null;
		if (args.Count != 1 || args["records"] is not JsonArray records || records.Count is < 1 or > 16)
		{
			error = "bad_args: nfc.write takes {records} with 1 to 16 records";
			return null;
		}
		for (var i = 0; i < records.Count; i++)
		{
			var issue = records[i] is JsonObject r ? RecordIssue(r) : "not an object";
			if (issue is null) continue;
			error = $"bad_args: record {i}: {issue}";
			return null;
		}
		return (JsonArray)records.DeepClone();
	}

	static string? RecordIssue(JsonObject r)
	{
		string? Str(string key) => r[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
		var type = Str("recordType");
		if (type is null) return "no recordType";
		var data = type == "mime" || External.IsMatch(type);
		string[] keys = type switch
		{
			"text" => ["recordType", "text", "lang", "encoding"],
			"url" or "absolute-url" => ["recordType", "text"],
			"empty" => ["recordType"],
			"mime" => ["recordType", "mediaType", "text", "hex"],
			_ when data => ["recordType", "text", "hex"],
			_ => [],
		};
		if (keys.Length == 0) return $"type {type} is not one the app writes";
		foreach (var (key, value) in r)
		{
			if (!keys.Contains(key)) return $"a {type} record takes no key {key}";
			if (value is not JsonValue v || !v.TryGetValue<string>(out _)) return $"{key} is not a string";
		}
		if (type is ("text" or "url" or "absolute-url") && Str("text") is null) return $"a {type} record needs text";
		if (type == "mime" && (Str("mediaType") is not { } media || !Media.IsMatch(media))) return "a mime record needs a mediaType";
		if (data && (Str("text") is null) == (Str("hex") is null)) return $"a {type} record holds exactly one of text and hex";
		if (Str("hex") is { } hex && !LowerHex.IsMatch(hex)) return "hex is lower-case hex bytes";
		if (Str("lang") is { } lang && !Lang.IsMatch(lang)) return "lang is a language tag";
		if (Str("encoding") is { } encoding && encoding is not ("utf-8" or "utf-16")) return "encoding is utf-8 or utf-16";
		if (type is ("url" or "absolute-url") && !Uri.TryCreate(Str("text"), UriKind.Absolute, out _)) return "a URI record holds an absolute URI";
		return null;
	}

	static string Cut(string text, int max) => text.Length <= max ? text : text[..max];
}

/// <summary>A pairing: the origin it belongs to, the device's id, its token and its hard cap.</summary>
public sealed record Pairing(string Origin, string DeviceId, string Token, DateTimeOffset HardCapAt);

/// <summary>What a realtime socket frame means to the device.</summary>
public abstract record Frame
{
	public static readonly Frame Ignored = new IgnoredFrame();
	public static readonly Frame Ping = new PingFrame();
}

/// <summary>A frame the device does nothing with.</summary>
public sealed record IgnoredFrame : Frame;

/// <summary>The socket's keepalive: the device answers with <see cref="DeviceProtocol.Pong"/>.</summary>
public sealed record PingFrame : Frame;

/// <summary>device.call_requested for this device: a call to run before its deadline.</summary>
public sealed record CallRequest(string Call, string Method, JsonObject Args, DateTimeOffset Deadline) : Frame;

/// <summary>device.call_done or device.call_failed for this device: core ended the call.</summary>
public sealed record CallEnded(string Call) : Frame;
