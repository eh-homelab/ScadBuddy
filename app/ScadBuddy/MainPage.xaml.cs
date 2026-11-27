namespace ScadBuddy;

/// <summary>
/// ScadBuddy's own /scan page (#7905, spec 2026-11-26: the app's screens are the web page). The
/// page is loaded from the ScadBuddy URL the person sets once; links to another origin open in
/// the browser, so the NFC bridge is never offered to another site.
/// </summary>
public partial class MainPage : ContentPage
{
	const string UrlKey = "scadbuddy.url";
	const string DefaultUrl = "https://scadbuddy.internal.nullreference.io/scan";

	string url = Preferences.Get(UrlKey, DefaultUrl);

	public MainPage()
	{
		InitializeComponent();
		Web.Navigating += OnNavigating;
		Web.HandlerChanged += (_, _) =>
		{
			if (Web.Handler is null) return;
#if ANDROID
			var platform = (Android.Webkit.WebView)Web.Handler.PlatformView!;
			platform.Settings.SetGeolocationEnabled(true);
			platform.SetWebChromeClient(new ScanChromeClient((Microsoft.Maui.Handlers.IWebViewHandler)Web.Handler, new HashSet<string>([OriginOf(url)], StringComparer.OrdinalIgnoreCase)));
			try
			{
				NfcBridge.Attach(platform, [OriginOf(url)]);
			}
			catch (Exception error)
			{
				Dispatcher.Dispatch(() => DisplayAlertAsync("NFC is not available", error.Message, "OK"));
			}
#endif
			Web.Source = url;
			if (DeepLink.Take() is { } first) Dispatcher.Dispatch(() => Scan(first));
		};
		// A cold start's link was raised after the handler took it (MAUI builds the page inside
		// base.OnCreate, before MainActivity raises it), so take it again once subscribed.
		Loaded += (_, _) =>
		{
			DeepLink.Opened += Scan;
			if (DeepLink.Take() is { } late) Scan(late);
		};
		Unloaded += (_, _) => DeepLink.Opened -= Scan;
	}

	const string OriginsKey = "scadbuddy.scanOrigins";

	/// <summary>
	/// Whether a scan link may send a read or a photo to <paramref name="origin"/>. Any page can
	/// make a scan link, so only the app's own ScadBuddy, or one the person accepted here once, gets
	/// the tag's bytes or the camera's photo.
	/// </summary>
	async Task<bool> Trusted(string origin)
	{
		var accepted = Preferences.Get(OriginsKey, "").Split('\n', StringSplitOptions.RemoveEmptyEntries).ToHashSet(StringComparer.OrdinalIgnoreCase);
		if (string.Equals(origin, OriginOf(url), StringComparison.OrdinalIgnoreCase) || accepted.Contains(origin)) return true;
		var yes = await DisplayAlertAsync("Send scans to this server?", $"A link asks this app to send a tag read or a photo to\n{origin}\n\nThis app's ScadBuddy is\n{OriginOf(url)}\n\nThey differ. Accept only a ScadBuddy you run. Changing the ScadBuddy address forgets every server accepted here.", "Accept", "Cancel");
		if (!yes) return false;
		accepted.Add(origin);
		Preferences.Set(OriginsKey, string.Join('\n', accepted));
		return true;
	}

	static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(30) };

	/// <summary>Does the scan a link asked for, posts it to the link's result address, and goes back to the browser.</summary>
	async void Scan(ScanJob job)
	{
#if ANDROID
		if (!await Trusted(DeepLink.OriginOf(job.Result))) return;
		if (job.Kind == "nfc")
		{
			NfcBridge.Instance.StartJob(job.Result);
			return;
		}
		try
		{
			var photo = await MediaPicker.Default.CapturePhotoAsync(new MediaPickerOptions { Title = "Photo of the code" });
			if (photo is null) return;
			await using var stream = await photo.OpenReadAsync();
			using var body = new StreamContent(stream);
			body.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue(photo.ContentType ?? "image/jpeg");
			using var answer = await Http.PostAsync(job.Result, body);
			if (!answer.IsSuccessStatusCode)
			{
				await DisplayAlertAsync("Not sent", $"ScadBuddy answered {(int)answer.StatusCode}. Start the scan again from the page.", "OK");
				return;
			}
			NfcBridge.Notify("Sent. Back to the page.");
			Platform.CurrentActivity?.MoveTaskToBack(true);
		}
		catch (Exception error)
		{
			await DisplayAlertAsync("Not sent", error.Message, "OK");
		}
#endif
	}

	static string OriginOf(string address) => DeepLink.OriginOf(new Uri(address));

	void OnNavigating(object? sender, WebNavigatingEventArgs e)
	{
		if (!Uri.TryCreate(e.Url, UriKind.Absolute, out var target)) return;
		if (target.Scheme is not ("http" or "https")) return;
		if (string.Equals(target.GetLeftPart(UriPartial.Authority), OriginOf(url), StringComparison.OrdinalIgnoreCase)) return;
		e.Cancel = true;
		_ = Launcher.OpenAsync(target);
	}

	async void OnServerClicked(object? sender, EventArgs e)
	{
		var entered = await DisplayPromptAsync(
			"ScadBuddy address",
			"The page this app opens. NFC works only on this address's site. The app restarts its page after a change.",
			initialValue: url,
			keyboard: Keyboard.Url);
		if (string.IsNullOrWhiteSpace(entered)) return;
		if (!Uri.TryCreate(entered.Trim(), UriKind.Absolute, out var parsed) || parsed.Scheme != "https")
		{
			await DisplayAlertAsync("Not changed", "Give a full https:// address.", "OK");
			return;
		}
		Preferences.Set(UrlKey, parsed.ToString());
		Preferences.Remove(OriginsKey);
		// Trusted() compares with this field: the old origin stops being the app's own at once.
		url = parsed.ToString();
		await DisplayAlertAsync("Saved", "Close the app and open it again to use the new address.", "OK");
	}

	async void OnTelemetryClicked(object? sender, EventArgs e)
	{
		var entered = await DisplayPromptAsync(
			"Telemetry",
			"The OpenTelemetry collector this app sends traces, metrics and logs to (OTLP/HTTP base URL). Leave it empty to send nothing. The app uses the change after a restart.",
			initialValue: Telemetry.Endpoint,
			keyboard: Keyboard.Url);
		if (entered is null) return;
		entered = entered.Trim();
		if (entered.Length > 0 && (!Uri.TryCreate(entered, UriKind.Absolute, out var parsed) || parsed.Scheme is not ("http" or "https")))
		{
			await DisplayAlertAsync("Not changed", "Give a full http:// or https:// address, or leave it empty.", "OK");
			return;
		}
		Preferences.Set(Telemetry.EndpointKey, entered);
		await DisplayAlertAsync("Saved", entered.Length == 0 ? "Close the app and open it again to stop telemetry." : "Close the app and open it again to use the new collector.", "OK");
	}

	void OnReloadClicked(object? sender, EventArgs e) => Web.Reload();

	protected override bool OnBackButtonPressed()
	{
		if (Web.CanGoBack)
		{
			Web.GoBack();
			return true;
		}
		return base.OnBackButtonPressed();
	}
}
