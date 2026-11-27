namespace ScadBuddy;

// The pure part of DeepLink: which links are scan links, and the origin a result goes to. It uses
// no MAUI type, so ScadBuddy.Tests compiles this file on its own (net10.0).
public static partial class DeepLink
{
	public const string Scheme = "scadbuddy";
	public const string Host = "scan";

	/// <summary>The scan a link asks for, or null when it is not a ScadBuddy scan link.</summary>
	public static ScanJob? TargetOf(Uri? link)
	{
		if (link is null || link.Scheme != Scheme || link.Host != Host) return null;
		var query = System.Web.HttpUtility.ParseQueryString(link.Query);
		var kind = query["kind"] ?? "nfc";
		if (kind is not ("nfc" or "code")) return null;
		return Uri.TryCreate(query["url"], UriKind.Absolute, out var target) && target.Scheme == "https" && target.UserInfo.Length == 0 ? new ScanJob(kind, target) : null;
	}

	/// <summary>The origin with its host in ASCII (punycode), so a look-alike host never reads as another.</summary>
	public static string OriginOf(Uri address) => $"{address.Scheme}://{address.IdnHost}{(address.IsDefaultPort ? "" : $":{address.Port}")}";
}

/// <summary>A scan a link asked for: <c>nfc</c> or <c>code</c>, and where its result goes.</summary>
public sealed record ScanJob(string Kind, Uri Result);
