using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace ScadBuddy;

/// <summary>
/// The app's OpenTelemetry (#7905): traces, metrics and logs over OTLP/HTTP, as
/// tools/go/bambuddy-fs sends its own. The collector is the homelab's unless the person sets
/// another base URL, or none, with the app's Telemetry button. Off, no provider is built and
/// nothing touches the network. Setup makes no network call; export runs in the background.
/// </summary>
public static class Telemetry
{
	/// <summary>The homelab's collector (eh-homelab/clusters#2221), bambuddy-fs's default. It takes no credentials.</summary>
	public const string DefaultEndpoint = "https://otlp.internal.nullreference.io";

	/// <summary>The resource's service.name, and so the Prometheus job label.</summary>
	public const string ServiceName = "scadbuddy-app";

	/// <summary>The Preferences key of the collector's base URL. An empty value turns telemetry off.</summary>
	public const string EndpointKey = "scadbuddy.otel.endpoint";

	const string InstanceKey = "scadbuddy.otel.instance";

	public static readonly ActivitySource Source = new(ServiceName);
	public static readonly Meter Meter = new(ServiceName);

	static TracerProvider? tracerProvider;
	static MeterProvider? meterProvider;
	static IServiceProvider? services;

	/// <summary>The NFC bridge's logger. It is the no-op one until the app is built.</summary>
	public static ILogger Nfc { get; private set; } = NullLogger.Instance;

	/// <summary>The collector's base URL, or "" when telemetry is off.</summary>
	public static string Endpoint => Preferences.Get(EndpointKey, DefaultEndpoint).Trim();

	/// <summary>Builds the trace and metric providers and adds the OTLP log exporter, when an endpoint is set.</summary>
	public static void Start(ILoggingBuilder logging)
	{
		var endpoint = Endpoint;
		if (endpoint.Length == 0 || !Uri.TryCreate(endpoint, UriKind.Absolute, out var root)) return;
		var resource = NewResource();
		tracerProvider = Sdk.CreateTracerProviderBuilder()
			.SetResourceBuilder(resource)
			// bambuddy-fs's sampler: the Go SDK's default, parent-based always-on.
			.SetSampler(new ParentBasedSampler(new AlwaysOnSampler()))
			.AddSource(ServiceName)
			.AddOtlpExporter(o => Configure(o, root, "/v1/traces"))
			.Build();
		meterProvider = Sdk.CreateMeterProviderBuilder()
			.SetResourceBuilder(resource)
			.AddMeter(ServiceName)
			.AddOtlpExporter((o, reader) =>
			{
				Configure(o, root, "/v1/metrics");
				// Cumulative, as bambuddy-fs: Prometheus's OTLP receiver refuses delta.
				reader.TemporalityPreference = MetricReaderTemporalityPreference.Cumulative;
				reader.PeriodicExportingMetricReaderOptions.ExportIntervalMilliseconds = 60_000;
			})
			.Build();
		logging.AddOpenTelemetry(o =>
		{
			o.SetResourceBuilder(resource);
			o.IncludeFormattedMessage = true;
			o.AddOtlpExporter(e => Configure(e, root, "/v1/logs"));
		});
	}

	/// <summary>Takes the built app's loggers. Call it once, after the app is built.</summary>
	public static void Attach(IServiceProvider built)
	{
		services = built;
		Nfc = built.GetRequiredService<ILoggerFactory>().CreateLogger("ScadBuddy.Nfc");
	}

	/// <summary>
	/// Sends what is buffered, within 5 s, off the main thread. Android gives an app no clean
	/// exit, so the app calls this each time it leaves the screen.
	/// </summary>
	public static void Flush()
	{
		if (tracerProvider is null) return;
		_ = Task.Run(() =>
		{
			tracerProvider?.ForceFlush(5000);
			meterProvider?.ForceFlush(5000);
			services?.GetService<LoggerProvider>()?.ForceFlush(5000);
		});
	}

	static void Configure(OtlpExporterOptions options, Uri root, string signal)
	{
		options.Protocol = OtlpExportProtocol.HttpProtobuf;
		// With HTTP/protobuf, an endpoint set in code is used as is: the signal's path goes on it here.
		options.Endpoint = new Uri(root, root.AbsolutePath.TrimEnd('/') + signal);
	}

	/// <summary>
	/// service.name scadbuddy-app, the app's version, and service.instance.id a random id kept
	/// for this install, so two phones' series stay apart without a device name in them.
	/// </summary>
	static ResourceBuilder NewResource()
	{
		var instance = Preferences.Get(InstanceKey, "");
		if (instance.Length == 0)
		{
			instance = Guid.NewGuid().ToString();
			Preferences.Set(InstanceKey, instance);
		}
		return ResourceBuilder.CreateDefault()
			.AddService(ServiceName, serviceVersion: AppInfo.VersionString, autoGenerateServiceInstanceId: false, serviceInstanceId: instance);
	}
}
