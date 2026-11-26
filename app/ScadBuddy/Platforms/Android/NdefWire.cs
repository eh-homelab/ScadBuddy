using System.Text;
using System.Text.Json.Nodes;
using Android.Nfc;

namespace ScadBuddy;

/// <summary>
/// NDEF records between Android's `NdefRecord` and Web NFC's record shapes, as Chrome maps them
/// (https://w3c.github.io/web-nfc/, "Parsing content" and "Creating NDEF message"), so the page's
/// wire records (frontend/src/lib/webNfc.ts `toWire`, `toInit`) read and write as they do in Chrome.
/// </summary>
static class NdefWire
{
	static readonly string[] UriPrefixes =
	[
		"", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://",
		"sftp://", "smb://", "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://", "urn:", "pop:", "sip:", "sips:", "tftp:",
		"btspp://", "btl2cap://", "btgoep://", "tcpobex://", "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:",
		"urn:epc:", "urn:nfc:",
	];

	static string Hex(byte[]? bytes) => bytes is null ? "" : Convert.ToHexString(bytes).ToLowerInvariant();
	static string? Text(byte[]? bytes) => bytes is null || bytes.Length == 0 ? null : Encoding.UTF8.GetString(bytes);

	public static JsonObject Parse(NdefRecord record)
	{
		var type = record.GetTypeInfo() ?? [];
		var payload = record.GetPayload() ?? [];
		var o = new JsonObject { ["id"] = Text(record.GetId()), ["mediaType"] = null, ["encoding"] = null, ["lang"] = null };
		switch (record.Tnf)
		{
			case NdefRecord.TnfEmpty:
				o["recordType"] = "empty";
				o["dataHex"] = null;
				break;
			case NdefRecord.TnfWellKnown when type.SequenceEqual("T"u8.ToArray()) && payload.Length > 0:
				var status = payload[0];
				var langLength = status & 0x3F;
				o["recordType"] = "text";
				o["encoding"] = (status & 0x80) != 0 ? "utf-16" : "utf-8";
				o["lang"] = Encoding.ASCII.GetString(payload, 1, Math.Min(langLength, payload.Length - 1));
				o["dataHex"] = Hex(payload.Skip(1 + langLength).ToArray());
				break;
			case NdefRecord.TnfWellKnown when type.SequenceEqual("U"u8.ToArray()) && payload.Length > 0:
				var prefix = payload[0] < UriPrefixes.Length ? UriPrefixes[payload[0]] : "";
				o["recordType"] = "url";
				o["dataHex"] = Hex(Encoding.UTF8.GetBytes(prefix + Encoding.UTF8.GetString(payload, 1, payload.Length - 1)));
				break;
			case NdefRecord.TnfWellKnown when type.SequenceEqual("Sp"u8.ToArray()):
				o["recordType"] = "smart-poster";
				o["dataHex"] = Hex(payload);
				break;
			case NdefRecord.TnfWellKnown:
				o["recordType"] = ":" + Encoding.ASCII.GetString(type);
				o["dataHex"] = Hex(payload);
				break;
			case NdefRecord.TnfMimeMedia:
				o["recordType"] = "mime";
				o["mediaType"] = Encoding.ASCII.GetString(type);
				o["dataHex"] = Hex(payload);
				break;
			case NdefRecord.TnfAbsoluteUri:
				o["recordType"] = "absolute-url";
				o["dataHex"] = Hex(type);
				break;
			case NdefRecord.TnfExternalType:
				o["recordType"] = Encoding.ASCII.GetString(type);
				o["dataHex"] = Hex(payload);
				break;
			default:
				o["recordType"] = "unknown";
				o["dataHex"] = Hex(payload);
				break;
		}
		return o;
	}

	public static NdefRecord[] Build(JsonArray records, string lang)
	{
		if (records.Count == 0) throw new ArgumentException("A message needs at least one record.");
		return records.Select(r => One(r!.AsObject(), lang)).ToArray();
	}

	static byte[] DataBytes(JsonObject init) =>
		init["text"] is { } text ? Encoding.UTF8.GetBytes(text.GetValue<string>())
		: init["hex"] is { } hex ? Convert.FromHexString(hex.GetValue<string>())
		: [];

	static NdefRecord One(JsonObject init, string lang)
	{
		var recordType = init["recordType"]!.GetValue<string>();
		var id = init["id"] is { } idNode ? Encoding.UTF8.GetBytes(idNode.GetValue<string>()) : null;
		switch (recordType)
		{
			case "empty":
				return new NdefRecord(NdefRecord.TnfEmpty, null, null, null);
			case "text":
				var language = init["lang"]?.GetValue<string>() ?? lang;
				var langBytes = Encoding.ASCII.GetBytes(language);
				var utf16 = init["encoding"]?.GetValue<string>() is "utf-16" or "utf-16be" or "utf-16le";
				var body = init["text"] is { } text
					? (utf16 ? Encoding.BigEndianUnicode : Encoding.UTF8).GetBytes(text.GetValue<string>())
					: DataBytes(init);
				var payload = new byte[1 + langBytes.Length + body.Length];
				payload[0] = (byte)((utf16 ? 0x80 : 0) | langBytes.Length);
				langBytes.CopyTo(payload, 1);
				body.CopyTo(payload, 1 + langBytes.Length);
				return new NdefRecord(NdefRecord.TnfWellKnown, "T"u8.ToArray(), id, payload);
			case "url":
				var uri = NdefRecord.CreateUri(init["text"]?.GetValue<string>() ?? Encoding.UTF8.GetString(DataBytes(init)))!;
				return id is null ? uri : new NdefRecord(uri.Tnf, uri.GetTypeInfo(), id, uri.GetPayload());
			case "absolute-url":
				return new NdefRecord(NdefRecord.TnfAbsoluteUri, DataBytes(init), id, null);
			case "mime":
				var media = init["mediaType"]?.GetValue<string>() ?? "application/octet-stream";
				return new NdefRecord(NdefRecord.TnfMimeMedia, Encoding.ASCII.GetBytes(media), id, DataBytes(init));
			case "smart-poster":
				var nested = init["records"] is JsonArray inner
					? new NdefMessage(Build(inner, lang)).ToByteArray()
					: DataBytes(init);
				return new NdefRecord(NdefRecord.TnfWellKnown, "Sp"u8.ToArray(), id, nested);
			case "unknown":
				return new NdefRecord(NdefRecord.TnfUnknown, null, id, DataBytes(init));
			default:
				if (recordType.StartsWith(':'))
					return new NdefRecord(NdefRecord.TnfWellKnown, Encoding.ASCII.GetBytes(recordType[1..]), id, DataBytes(init));
				if (recordType.Contains(':'))
					return new NdefRecord(NdefRecord.TnfExternalType, Encoding.ASCII.GetBytes(recordType), id, DataBytes(init));
				throw new ArgumentException($"Web NFC has no record type {recordType}.");
		}
	}
}
