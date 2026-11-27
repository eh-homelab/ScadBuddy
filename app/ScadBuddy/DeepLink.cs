namespace ScadBuddy;

/// <summary>
/// "Scan with the ScadBuddy app" (#7905). A web page, on any device, makes a scan job and opens
/// <c>scadbuddy://scan?kind=nfc|code&amp;url=&lt;one-time result address&gt;</c>. For <c>nfc</c> the app
/// reads the next tag and posts the read; for <c>code</c> it takes a photo of a QR code or barcode and
/// posts the image. The server decodes both and ends the job; the app goes back to the browser,
/// and the page sees the job end. The address is a single-use, short-lived capability, as a presigned
/// upload is: the app needs no sign-in. Any page can make such a link, so the app sends a scan only
/// to its own ScadBuddy, or to an origin the person accepted once (<c>MainPage.Trusted</c>).
/// </summary>
public static partial class DeepLink
{
	static ScanJob? pending;

	/// <summary>Raised on the main thread with the result address of a link that came while the app was open.</summary>
	public static event Action<ScanJob>? Opened;

	public static void Raise(Uri? link)
	{
		var target = TargetOf(link);
		if (target is null) return;
		if (Opened is { } opened) MainThread.BeginInvokeOnMainThread(() => opened(target));
		else pending = target;
	}

	/// <summary>The link the app was started with, once.</summary>
	public static ScanJob? Take()
	{
		var target = pending;
		pending = null;
		return target;
	}
}
