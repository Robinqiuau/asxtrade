from django.shortcuts import render, get_object_or_404
from django.core.paginator import Paginator
from django.views.generic import FormView, UpdateView, DeleteView, CreateView
from django.http import HttpResponseRedirect, Http404, HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django import forms
from django.views.generic.list import MultipleObjectTemplateResponseMixin, MultipleObjectMixin
from bson.objectid import ObjectId
from collections import defaultdict
from app.models import *
from app.mixins import SearchMixin
from app.forms import SectorSearchForm, DividendSearchForm, CompanySearchForm
from app.analysis import relative_strength
from app.plots import *
import pylru
import numpy as np


def info(request, msg):
    assert request is not None
    assert len(msg) > 0
    messages.info(request, msg, extra_tags="alert alert-secondary", fail_silently=True)
    print(msg)

def warning(request, msg):
    assert request is not None
    assert len(msg) > 0
    messages.warning(request, msg, extra_tags="alert alert-warning", fail_silently=True)
    print("WARNING: {}".format(msg))

def add_messages(request, context):
    assert request is not None
    assert context is not None
    as_at = context.get('most_recent_date', None)
    sector = context.get('sector', None)
    if as_at:
        info(request, 'Prices current as at {}.'.format(as_at))
    if sector:
        info(request, "Only stocks from {} are shown.".format(sector))

class SectorSearchView(SearchMixin, LoginRequiredMixin, MultipleObjectMixin, MultipleObjectTemplateResponseMixin, FormView):
    form_class = SectorSearchForm
    template_name = "search_form.html" # generic template, not specific to this view
    action_url = '/search/by-sector'
    paginate_by = 50
    ordering = ('-annual_dividend_yield', 'asx_code') # keep pagination happy, but not used by get_queryset()
    sector_name = None
    as_at_date = None
    sentiment_data = None
    n_days = 30
    n_top_bottom = 20

    def render_to_response(self, context):
        context.update({ # NB: we use update() to not destroy page_obj
            'sector': self.sector_name,
            'most_recent_date': self.as_at_date,
            'sentiment_heatmap': self.sentiment_data,
            'watched': user_watchlist(self.request.user), # to highlight top10/bottom10 bookmarks correctly
            'n_top_bottom': self.n_top_bottom,
            'best_ten': self.top10,
            'worst_ten': self.bottom10,
            'title': 'Find by company sector',
            'sentiment_heatmap_title': "{}: past {} days".format(self.sector_name, self.n_days)
        })
        add_messages(self.request, context)
        return super().render_to_response(context)

    def get_queryset(self, **kwargs):
       if kwargs == {}:
           self.top10 = self.bottom10 = None
           return Quotation.objects.none()
       assert 'sector' in kwargs and len(kwargs['sector']) > 0
       self.sector_name = kwargs['sector']
       all_dates = all_available_dates()
       wanted_stocks = set(all_sector_stocks(self.sector_name))
       wanted_dates = desired_dates(start_date=self.n_days)
       self.sentiment_data, df, top10, bottom10, _ = plot_heatmap(wanted_stocks, all_dates=wanted_dates, n_top_bottom=self.n_top_bottom)
       self.top10 = top10
       self.bottom10 = bottom10
       if any(['best10' in kwargs, 'worst10' in kwargs]):
           wanted = set()
           restricted = False
           if kwargs.get('best10', False):
               wanted = wanted.union(top10.index)
               restricted = True
           if kwargs.get('worst10', False):
               wanted = wanted.union(bottom10.index)
               restricted = True
           if restricted:
               wanted_stocks = wanted_stocks.intersection(wanted)
           # FALLTHRU...
       self.as_at_date = all_dates[-1]

       print("Looking for {} companies as at {}".format(len(wanted_stocks), self.as_at_date))
       self.wanted_stocks = wanted_stocks
       results = Quotation.objects.filter(asx_code__in=wanted_stocks, fetch_date=self.as_at_date) \
                                  .exclude(error_code='id-or-code-invalid')
       results = results.order_by(*self.ordering)
       return results

sector_search = SectorSearchView.as_view()

class DividendYieldSearch(SearchMixin, LoginRequiredMixin, MultipleObjectMixin, MultipleObjectTemplateResponseMixin, FormView):
    form_class = DividendSearchForm
    template_name = "search_form.html" # generic template, not specific to this view
    action_url = '/search/by-yield'
    paginate_by = 50
    ordering = ('-annual_dividend_yield', 'asx_code') # keep pagination happy, but not used by get_queryset()
    as_at_date = None
    n_top_bottom = 20

    def render_to_response(self, context):
        qs = context['paginator'].object_list.all() # all() to get a fresh queryset instance
        qs = list(qs.values_list('asx_code', flat=True))
        if len(qs) == 0:
            warning(self.request, "No stocks to report")
            sentiment_data, df, top10, bottom10, n_stocks = (None, None, None, None, 0)
        else:
            sentiment_data, df, top10, bottom10, n_stocks = plot_heatmap(qs, n_top_bottom=self.n_top_bottom)
        context.update({
           'most_recent_date': self.as_at_date,
           'sentiment_heatmap': sentiment_data,
           'watched': user_watchlist(self.request.user), # to ensure bookmarks are correct
           'n_top_bottom': self.n_top_bottom,
           'best_ten': top10,
           'worst_ten': bottom10,
           'title': 'Find by dividend yield or P/E',
           'sentiment_heatmap_title': "Recent sentiment: {} total stocks".format(n_stocks),
        })
        add_messages(self.request, context)
        return super().render_to_response(context)

    def get_queryset(self, **kwargs):
        if kwargs == {}:
            return Quotation.objects.none()

        self.as_at_date = latest_quotation_date('ANZ')
        min_yield = kwargs.get('min_yield') if 'min_yield' in kwargs else 0.0
        max_yield = kwargs.get('max_yield') if 'max_yield' in kwargs else 10000.0
        results = Quotation.objects.filter(fetch_date=self.as_at_date) \
                              .filter(annual_dividend_yield__gte=min_yield) \
                              .filter(annual_dividend_yield__lte=max_yield)
        if 'min_pe' in kwargs:
            results = results.filter(pe__gte=kwargs.get('min_pe'))
        if 'max_pe' in kwargs:
            results = results.filter(pe__lt=kwargs.get('max_pe'))

        results = results.order_by(*self.ordering)

        return results

dividend_search = DividendYieldSearch.as_view()


class CompanySearch(DividendYieldSearch):
    form_class = CompanySearchForm
    action_url = "/search/by-company"
    paginate_by = 50

    def render_to_response(self, context, **kwargs):
        result = super().render_to_response(context, **kwargs)
        context['title'] = 'Find by company name or activity'
        return result

    def get_queryset(self, **kwargs):
        if kwargs == {} or not any(['name' in kwargs, 'activity' in kwargs]):
            return Quotation.objects.none()
        wanted_name = kwargs.get('name', '')
        wanted_activity = kwargs.get('activity', '')
        matching_companies = find_named_companies(wanted_name, wanted_activity)
        print("Showing results for {} companies".format(len(matching_companies)))
        self.as_at_date = latest_quotation_date('ANZ')
        results, latest_date = latest_quote(tuple(matching_companies))
        results = results.order_by(*self.ordering)
        return results

company_search = CompanySearch.as_view()

@login_required
def all_stocks(request):
   all_dates = all_available_dates()
   if len(all_dates) < 1:
       raise Http404("No ASX price data available!")
   ymd = all_dates[-1]
   assert isinstance(ymd, str) and len(ymd) > 8
   qs = Quotation.objects.filter(fetch_date=ymd) \
                         .exclude(asx_code__isnull=True) \
                         .exclude(last_price__isnull=True) \
                         .exclude(volume=0) \
                         .order_by('-annual_dividend_yield', '-last_price', '-volume')
   assert qs is not None
   paginator = Paginator(qs, 50)
   page_number = request.GET.get('page', 1)
   page_obj = paginator.get_page(page_number)
   context = {
       "title": "ASX stocks by dividend yield",
       "page_obj": page_obj,
       "most_recent_date": ymd,
       "watched": user_watchlist(request.user)
   }
   return render(request, "all_stocks.html", context=context)

@login_required
def show_stock(request, stock=None, sector_n_days=90):
   """
   Displays a view of a single stock via the stock_view.html template and associated state
   """
   validate_stock(stock)
   validate_user(request.user)
   all_stock_quotes_df  = all_quotes(stock, all_dates=desired_dates(start_date=sector_n_days))
   securities = Security.objects.filter(asx_code=stock)
   company_details = CompanyDetails.objects.filter(asx_code=stock).first()
   if company_details is None:
       warning(request, "No details available for {}".format(stock))

   if len(all_stock_quotes_df) < 14:  # RSI requires at least 14 prices to plot so reject recently added stocks
       raise Http404("Insufficient price quotes for {} - only {}".format(stock, len(all_stock_quotes_df)))
   fig = make_rsi_plot(stock, all_stock_quotes_df)

   # show sector performance over past 3 months
   window_size = 14 # since must have a full window before computing momentum
   all_dates = desired_dates(start_date=sector_n_days+window_size)
   sector = company_details.sector_name if company_details else None
   sector_companies = all_sector_stocks(sector) if sector else []
   if len(sector_companies) > 0:
      cip = company_prices(sector_companies, all_dates=all_dates, field_name='change_in_percent')
      cip = cip.fillna(0.0)
      rows = []
      cum_sum = defaultdict(float)
      stock_versus_sector = []
      # identify the best performing stock in the sector and add it to the stock_versus_sector rows...
      best_stock_in_sector = cip.sum(axis=1).nlargest(1).index[0]
      for day in sorted(cip.columns, key=lambda k: datetime.strptime(k, "%Y-%m-%d")):
          for asx_code, daily_change in cip[day].iteritems():
              cum_sum[asx_code] += daily_change
          n_pos = len(list(filter(lambda t: t[1] >= 5.0, cum_sum.items())))
          n_neg = len(list(filter(lambda t: t[1] < -5.0, cum_sum.items())))
          n_unchanged = len(cip) - n_pos - n_neg
          rows.append({ 'n_pos': n_pos, 'n_neg': n_neg, 'n_unchanged': n_unchanged, 'date': day})
          stock_versus_sector.append({ 'group': stock, 'date': day, 'value': cum_sum[stock] })
          stock_versus_sector.append({ 'group': 'sector_average', 'date': day, 'value': pd.Series(cum_sum).mean() })
          if stock != best_stock_in_sector:
              stock_versus_sector.append({ 'group': '{} (best in {})'.format(best_stock_in_sector, sector), 'value': cum_sum[best_stock_in_sector], 'date': day})
      df = pd.DataFrame.from_records(rows)
      sector_momentum_data = make_momentum_plot(df, sector, window_size=window_size)

      # company versus sector performance
      stock_versus_sector_df = pd.DataFrame.from_records(stock_versus_sector)
      c_vs_s_plot = plot_company_versus_sector(stock_versus_sector_df, stock, sector)

      # key indicator performance over past 90 days (for now): pe, eps, yield etc.
      key_indicator_plot = plot_key_stock_indicators(all_stock_quotes_df, stock)
   else:
      c_vs_s_plot = sector_momentum_data = key_indicator_plot = None

   # plot the price over last 600 days in monthly blocks ie. max 24 bars which is still readable
   monthly_maximum_plot = plot_best_monthly_price_trend(all_quotes(stock, all_dates=desired_dates(start_date=600)))

   # populate template and render HTML page with context
   context = {
       'rsi_data': fig,
       'asx_code': stock,
       'securities': securities,
       'cd': company_details,
       'sector_momentum_plot': sector_momentum_data,
       'sector_momentum_title': "{} sector stocks: {} day performance".format(sector, sector_n_days),
       'company_versus_sector_plot': c_vs_s_plot,
       'company_versus_sector_title': '{} vs. {} performance'.format(stock, sector),
       'key_indicators_plot': key_indicator_plot,
       'monthly_highest_price_plot_title': 'Maximum price each month trend',
       'monthly_highest_price_plot': monthly_maximum_plot,
   }
   return render(request, "stock_view.html", context=context)

def save_dataframe_to_file(df, filename, format):
    assert format in ('csv', 'excel', 'tsv', 'parquet')
    assert df is not None and len(df) > 0
    assert len(filename) > 0

    if format == 'csv':
        df.to_csv(filename)
        return 'text/csv'
    elif format == 'excel':
        df.to_excel(filename)
        return 'application/vnd.ms-excel'
    elif format == 'tsv':
        df.to_csv(filename, sep='\t')
        return 'text/tab-separated-values'
    elif format == 'parquet':
        df.to_parquet(filename)
        return 'application/octet-stream' # for now, but must be something better...
    else:
        raise ValueError("Unsupported format {}".format(format))

def get_dataset(dataset_wanted):
    assert dataset_wanted in ('market_sentiment')

    if dataset_wanted == 'market_sentiment':
        _, df, _, _, _ = plot_heatmap(None, all_dates=desired_dates(21), n_top_bottom=20)
        return df
    else:
        raise ValueError("Unsupported dataset {}".format(dataset_wanted))

@login_required
def download_data(request, dataset=None, format='csv'):
    validate_user(request.user)
    import tempfile
    with tempfile.NamedTemporaryFile() as fh:
        df = get_dataset(dataset)
        content_type = save_dataframe_to_file(df, fh.name, format)
        fh.seek(0)
        response = HttpResponse(fh.read(), content_type=content_type)
        response['Content-Disposition'] = 'inline; filename=temp.{}'.format(format)
        return response

@login_required
def market_sentiment(request, n_days=21, n_top_bottom=20):
    validate_user(request.user)
    assert n_days > 0
    assert n_top_bottom > 0
    all_dates = desired_dates(n_days)
    sentiment_heatmap_data, df, top10, bottom10, n = plot_heatmap(None, all_dates=all_dates, n_top_bottom=n_top_bottom)
    sector_performance_plot = plot_market_wide_sector_performance(desired_dates(180))

    context = {
       'sentiment_data': sentiment_heatmap_data,
       'n_days': n_days,
       'n_stocks_plotted': n,
       'n_top_bottom': n_top_bottom,
       'best_ten': top10,
       'worst_ten': bottom10,
       'watched': user_watchlist(request.user),
       'sector_performance': sector_performance_plot,
       'sector_performance_title': '180 day cumulative sector avg. performance',
       'title': "Market sentiment over past {} days".format(n_days)
    }
    return render(request, 'market_sentiment_view.html', context=context)

@login_required
def show_etfs(request):
    validate_user(request.user)
    matching_codes = all_etfs()
    return show_matching_companies(matching_codes,
        "Exchange Traded funds over past 300 days",
        "Sentiment for ETFs",
        None,
        request
    )

@login_required
def show_increasing_eps_stocks(request):
    validate_user(request.user)
    matching_companies = increasing_eps(None)
    return show_matching_companies(matching_companies,
                "Stocks with increasing EPS over past 300 days",
                "Sentiment for selected stocks",
                None, # dont show purchases on this view
                request
    )

@login_required
def show_increasing_yield_stocks(request):
    validate_user(request.user)
    matching_companies = increasing_yield(None)
    return show_matching_companies(matching_companies,
                "Stocks with increasing yield over past 300 days",
                "Sentiment for selected stocks",
                None,
                request
    )

@login_required
def show_trends(request):
    validate_user(request.user)
    watchlist_stocks = user_watchlist(request.user)
    all_dates = desired_dates(start_date=300) # last 300 days
    cip = company_prices(watchlist_stocks, all_dates=all_dates, field_name='change_in_percent', fail_on_missing=False)
    trends = calculate_trends(cip, watchlist_stocks, all_dates)
    # for now we only plot trending companies... too slow and unreadable to load the page otherwise!
    cip = rank_cumulative_change(cip.filter(trends.keys(), axis='index'), all_dates=all_dates)
    trending_companies_plot = plot_company_rank(cip)
    context = {
        'watchlist_trends': trends,
        'trending_companies_plot': trending_companies_plot,
        'trending_companies_plot_title': 'Trending watchlist companies by rank (past 300 days)'
    }
    return render(request, 'trends.html', context=context)

@login_required
def show_purchase_performance(request):
    purchase_buy_dates = []
    purchases = []
    stocks = []
    for stock, purchases_for_stock in user_purchases(request.user).items():
        stocks.append(stock)
        for purchase in purchases_for_stock:
            purchase_buy_dates.append(purchase.buy_date)
            purchases.append(purchase)

    purchase_buy_dates = sorted(purchase_buy_dates)
    #print("earliest {} latest {}".format(purchase_buy_dates[0], purchase_buy_dates[-1]))

    all_dates = desired_dates(start_date=purchase_buy_dates[0])
    df = company_prices(stocks, all_dates=all_dates)
    rows = []
    stock_count = defaultdict(int)
    stock_cost = defaultdict(float)
    portfolio_cost = 0.0

    for d in all_dates:
        d = datetime.strptime(d, "%Y-%m-%d").date()
        d_str = str(d)
        if d_str not in df.columns: # not a trading day?
            continue
        purchases_to_date = filter(lambda vp: vp.buy_date <= d, purchases)
        for purchase in purchases_to_date:
            if purchase.buy_date == d:
               portfolio_cost += purchase.amount
               stock_count[purchase.asx_code] += purchase.n
               stock_cost[purchase.asx_code] += purchase.amount

        portfolio_worth = sum(map(lambda t: df.at[t[0], d_str] * t[1], stock_count.items()))

        # emit rows for each stock and aggregate portfolio
        for asx_code in stocks:
            cur_price = df.at[asx_code, d_str]
            if np.isnan(cur_price): # price missing? ok, skip record
                continue
            assert cur_price is not None and cur_price >= 0.0
            stock_worth = cur_price * stock_count[asx_code]

            rows.append({ 'portfolio_cost': portfolio_cost,
                    'portfolio_worth': portfolio_worth,
                    'portfolio_profit': portfolio_worth - portfolio_cost,
                    'stock_cost': stock_cost[asx_code],
                    'stock_worth': stock_worth,
                    'stock_profit': stock_worth - stock_cost[asx_code],
                    'date': d_str, 'stock': asx_code })

    portfolio_performance_figure, stock_performance_figure, \
       profit_contributors_figure = plot_portfolio(pd.DataFrame.from_records(rows))
    context = {
         'title': 'Portfolio performance',
         'portfolio_title': 'Overall',
         'portfolio_figure': portfolio_performance_figure,
         'stock_title': 'Stock',
         'stock_figure': stock_performance_figure,
         'profit_contributors': profit_contributors_figure,
    }
    return render(request, 'portfolio_trends.html', context=context)

def show_matching_companies(matching_companies, title, heatmap_title, user_purchases, request):
    """
    Support function to public-facing views to eliminate code redundancy
    """
    assert len(matching_companies) > 0
    assert isinstance(title, str) and isinstance(heatmap_title, str)

    print("Showing results for {} companies".format(len(matching_companies)))
    stocks_queryset, date = latest_quote(matching_companies)
    stocks_queryset = stocks_queryset.order_by('asx_code')

    # paginate results for 50 stocks per page
    paginator = Paginator(stocks_queryset, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.page(page_number)

    # add sentiment heatmap amongst watched stocks
    n_days = 30
    n_top_bottom = 20
    sentiment_heatmap_data, df, top10, bottom10, n = \
        plot_heatmap(matching_companies, all_dates=desired_dates(start_date=n_days), n_top_bottom=n_top_bottom)

    context = {
         "most_recent_date": latest_quotation_date('ANZ'),
         "page_obj": page_obj,
         "title": title,
         "watched": user_watchlist(request.user),
         'n_top_bottom': n_top_bottom,
         "best_ten": top10,
         "worst_ten": bottom10,
         "virtual_purchases": user_purchases,
         "sentiment_heatmap": sentiment_heatmap_data,
         "sentiment_heatmap_title": "{}: past {} days".format(heatmap_title, n_days)
    }
    add_messages(request, context)
    return render(request, 'all_stocks.html', context=context)

@login_required
def show_watched(request):
    validate_user(request.user)
    matching_companies = user_watchlist(request.user)
    purchases = user_purchases(request.user)

    return show_matching_companies(matching_companies,
               "Stocks you are watching",
               "Watched stock recent sentiment",
               purchases,
               request
    )

def redirect_to_next(request, fallback_next='/'):
    """
    Call this function in your view once you have deleted some database data: set the 'next' query href
    param to where the redirect should go to. If not specified '/' will be assumed. Not permitted to
    redirect to another site.
    """
    # redirect will trigger a redraw which will show the purchase since next will be the same page
    assert request is not None
    if request.GET is not None:
        next_href = request.GET.get('next', fallback_next)
        assert next_href.startswith('/') # PARANOIA: must be same origin
        return HttpResponseRedirect(next_href)
    else:
        return HttpResponseRedirect(fallback_next)

@login_required
def toggle_watched(request, stock=None):
    validate_stock(stock)
    validate_user(request.user)
    current_watchlist = user_watchlist(request.user)
    if stock in current_watchlist: # remove from watchlist?
        Watchlist.objects.filter(user=request.user, asx_code=stock).delete()
    else:
        w = Watchlist(user=request.user, asx_code=stock)
        w.save()
    return redirect_to_next(request)

class BuyVirtualStock(LoginRequiredMixin, CreateView):
    model = VirtualPurchase
    success_url = '/show/watched'
    form_class = forms.models.modelform_factory(VirtualPurchase,
                        fields=['asx_code', 'buy_date', 'price_at_buy_date', 'amount', 'n'],
                        widgets={"asx_code": forms.TextInput(attrs={'readonly': 'readonly'})})

    def form_valid(self, form):
        req = self.request
        resp = super().form_valid(form) # only if no exception raised do we save...
        self.object = form.save(commit=False)
        self.object.user = validate_user(req.user)
        self.object.save()
        info(req, "Saved purchase of {}".format(self.kwargs.get('stock')))
        return resp

    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        result['title'] = "Add {} purchase to watchlist".format(self.kwargs.get('stock'))
        return result

    def get_initial(self, **kwargs):
        stock = self.kwargs.get('stock')
        amount = self.kwargs.get('amount', 5000.0)
        user = self.request.user
        validate_stock(stock)
        validate_user(user)
        quote, latest_date = latest_quote(stock)
        cur_price = quote.last_price
        if cur_price >= 1e-6:
            return { 'asx_code': stock, 'user': user,
                     'buy_date': latest_date, 'price_at_buy_date': cur_price,
                     'amount': amount, 'n': int(amount / cur_price) }
        else:
            warning(self.request, "Cannot buy {} as its price is zero/unknown".format(stock))
            return {}

buy_virtual_stock = BuyVirtualStock.as_view()

class MyObjectMixin:
    """
    Retrieve the object by mongo _id for use by CRUD CBV views for VirtualPurchase's
    """
    def get_object(self, queryset=None):
        slug = self.kwargs.get('slug')
        purchase = VirtualPurchase.objects.mongo_find_one({ '_id': ObjectId(slug) })
        #print(purchase)
        purchase['id'] = purchase['_id']
        purchase.pop('_id', None)
        return VirtualPurchase(**purchase)

class EditVirtualStock(LoginRequiredMixin, MyObjectMixin, UpdateView):
    model = VirtualPurchase
    success_url = '/show/watched'
    form_class = forms.models.modelform_factory(VirtualPurchase,
                        fields=['asx_code', 'buy_date', 'price_at_buy_date', 'amount', 'n'],
                        widgets={"asx_code": forms.TextInput(attrs={ 'readonly': 'readonly' }),
                                 "buy_date": forms.DateInput()})

edit_virtual_stock = EditVirtualStock.as_view()

class DeleteVirtualPurchaseView(LoginRequiredMixin, MyObjectMixin, DeleteView):
    model = VirtualPurchase
    success_url = '/show/watched'

delete_virtual_stock = DeleteVirtualPurchaseView.as_view()
