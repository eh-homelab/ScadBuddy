using System.Text.RegularExpressions;

namespace ScadBuddy;

// The pure part of DeepLink: which links are pairing links, the origin a pairing goes to, and
// the question the app asks. It uses no MAUI type, so ScadBuddy.Tests compiles this file on its
// own (net10.0).
public static partial class DeepLink
{
	public const string Scheme = "scadbuddy";
	public const string Host = "pair";

	// Core's claim route (tools/go/core/internal/devices, Store.Public): the code is 16 random
	// bytes, base64url without padding.
	static readonly Regex ClaimPath = new("^/api/v1/devices/pairings/[A-Za-z0-9_-]{22}/claim$", RegexOptions.CultureInvariant);

	/// <summary>
	/// The claim address a link gives, or null when it is not a ScadBuddy pairing link:
	/// <c>scadbuddy://pair?url=https://&lt;host&gt;/api/v1/devices/pairings/&lt;code&gt;/claim</c>,
	/// with no user, query or fragment in the address.
	/// </summary>
	public static Uri? TargetOf(Uri? link)
	{
		if (link is null || link.Scheme != Scheme || link.Host != Host) return null;
		var query = System.Web.HttpUtility.ParseQueryString(link.Query);
		if (!Uri.TryCreate(query["url"], UriKind.Absolute, out var claim)) return null;
		if (claim.Scheme != "https" || claim.UserInfo.Length != 0 || claim.Query.Length != 0 || claim.Fragment.Length != 0) return null;
		return ClaimPath.IsMatch(claim.AbsolutePath) ? claim : null;
	}

	// A spool label's QR text (frontend labelSheet.ts labelPayload): no "//" and no host, so it
	// outlives a move of the app (#8550).
	static readonly Regex SpoolLabel = new("^scadbuddy:spool:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", RegexOptions.CultureInvariant);

	/// <summary>The spool id a spool label's link <c>scadbuddy:spool:&lt;uuid&gt;</c> names, in lower case, or null.</summary>
	public static string? SpoolOf(Uri? link)
	{
		if (link is null || link.Scheme != Scheme) return null;
		var m = SpoolLabel.Match(link.OriginalString);
		return m.Success ? m.Groups[1].Value.ToLowerInvariant() : null;
	}

	/// <summary>
	/// The script the app runs in the signed-in page for a spool label: it reads the spool, then
	/// opens <c>/x/S-&lt;n&gt;</c>, which offers a task that waits for the spool first (CodePage);
	/// when the read fails it opens the spool's page. <paramref name="spool"/> is a checked uuid.
	/// </summary>
	public static string OpenSpoolScript(string spool) =>
		"(async()=>{const id='" + spool + "';try{const r=await fetch('/api/v1/filament/spools/'+id,{credentials:'same-origin'});" +
		"if(r.ok){const s=await r.json();if(Number.isInteger(s.number)){location.assign('/x/S-'+s.number);return}}}catch(e){}" +
		"location.assign('/filament/spools/'+id)})()";

	/// <summary>The origin with its host in ASCII (punycode), so a look-alike host never reads as another.</summary>
	public static string OriginOf(Uri address) => $"{address.Scheme}://{address.IdnHost}{(address.IsDefaultPort ? "" : $":{address.Port}")}";

	/// <summary>
	/// The question the app asks before it pairs: the claim's origin in punycode, and the app's
	/// own set ScadBuddy origin too when the two differ.
	/// </summary>
	public static string PairQuestion(Uri claim, string ownOrigin)
	{
		var origin = OriginOf(claim);
		var question = $"Pair this phone with\n{origin}?\n\nThat server can then read and write NFC tags and take photos with this phone while the app is open.";
		if (!string.Equals(origin, ownOrigin, StringComparison.OrdinalIgnoreCase))
			question += $"\n\nThis app's ScadBuddy is\n{ownOrigin}\n\nThey differ. Pair only with a ScadBuddy you run.";
		return question;
	}
}
