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

	// The loaded page's address: its bridge, camera and links keep it until the app restarts.
	string url = Preferences.Get(UrlKey, DefaultUrl);
	// The set ScadBuddy's origin, which the pairing question names when a link's server differs:
	// the new address as soon as the person sets it, before the restart loads it.
	string ownOrigin = DeepLink.OriginOf(new Uri(Preferences.Get(UrlKey, DefaultUrl)));

	public MainPage()
	{
		InitializeComponent();
		Web.Navigating += OnNavigating;
		Web.Navigated += (_, e) =>
		{
			loaded = e.Result == WebNavigationResult.Success;
			if (loaded && waitingSpool is { } spool) OpenSpool(spool);
		};
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
			if (DeepLink.Take() is { } first) Dispatcher.Dispatch(() => Pair(first));
		};
		// A cold start's link was raised after the handler took it (MAUI builds the page inside
		// base.OnCreate, before MainActivity raises it), so take it again once subscribed.
		Loaded += (_, _) =>
		{
			DeepLink.Opened += Pair;
			DeepLink.SpoolOpened += OpenSpool;
			if (DeepLink.Take() is { } late) Pair(late);
			if (DeepLink.TakeSpool() is { } spool) OpenSpool(spool);
		};
		Unloaded += (_, _) =>
		{
			DeepLink.Opened -= Pair;
			DeepLink.SpoolOpened -= OpenSpool;
		};
	}

	bool loaded;
	string? waitingSpool;

	/// <summary>
	/// A spool label (#8550): the signed-in page reads the spool and opens <c>/x/S-&lt;n&gt;</c>, so a
	/// task that waits for the spool comes first. Before the first page has loaded, it waits for it.
	/// </summary>
	void OpenSpool(string spool)
	{
		if (!loaded)
		{
			waitingSpool = spool;
			return;
		}
		waitingSpool = null;
		_ = Web.EvaluateJavaScriptAsync(DeepLink.OpenSpoolScript(spool));
	}

	/// <summary>
	/// Pairs the phone with the server a pairing link names. Any page can make such a link, so the
	/// app always asks first, and names the claim's origin in punycode, and the app's own set
	/// address too when the two differ.
	/// </summary>
	async void Pair(Uri claim)
	{
#if ANDROID
		var origin = DeepLink.OriginOf(claim);
		if (!await DisplayAlertAsync("Pair this phone?", DeepLink.PairQuestion(claim, ownOrigin), "Pair", "Cancel")) return;
		try
		{
			await DeviceLink.Instance.Pair(claim);
			await DisplayAlertAsync("Paired", $"This phone is a device of\n{origin}\n\nKeep the app open while ScadBuddy uses its NFC or camera.", "OK");
		}
		catch (Exception error)
		{
			await DisplayAlertAsync("Not paired", error.Message, "OK");
		}
#endif
	}

	static string OriginOf(string address) => DeepLink.OriginOf(new Uri(address));

	void OnNavigating(object? sender, WebNavigatingEventArgs e)
	{
		if (!Uri.TryCreate(e.Url, UriKind.Absolute, out var target)) return;
		if (target.Scheme == DeepLink.Scheme)
		{
			// A pairing link the page shows as a tap link: the app asks, as for one from the browser;
			// a spool label's link opens the spool's code page.
			e.Cancel = true;
			DeepLink.Raise(target);
			return;
		}
		if (target.Scheme is not ("http" or "https")) return;
		// Both sides in punycode, so an international ScadBuddy host keeps its own links in the app.
		if (string.Equals(DeepLink.OriginOf(target), OriginOf(url), StringComparison.OrdinalIgnoreCase)) return;
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
		// The loaded page keeps the old address until the restart; the pairing question names the
		// new one as the app's own at once.
		ownOrigin = DeepLink.OriginOf(parsed);
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
