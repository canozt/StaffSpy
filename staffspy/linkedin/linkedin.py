"""
staffspy.linkedin.linkedin
~~~~~~~~~~~~~~~~~~~

This module contains routines to scrape LinkedIn.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, unquote

import requests

import staffspy.utils.utils as utils
from staffspy.utils.exceptions import TooManyRequests, BadCookies, GeoUrnNotFound
from staffspy.linkedin.contact_info import ContactInfoFetcher
from staffspy.linkedin.certifications import CertificationFetcher
from staffspy.linkedin.employee import EmployeeFetcher
from staffspy.linkedin.employee_bio import EmployeeBioFetcher
from staffspy.linkedin.experiences import ExperiencesFetcher
from staffspy.linkedin.languages import LanguagesFetcher
from staffspy.linkedin.schools import SchoolsFetcher
from staffspy.linkedin.skills import SkillsFetcher
from staffspy.utils.models import Staff
from staffspy.utils.utils import logger

import random
import time

class LinkedInScraper:
    employees_ep = "https://www.linkedin.com/voyager/api/graphql?variables=(start:{offset},query:(flagshipSearchIntent:SEARCH_SRP,{search}queryParameters:List({company_id}{location}(key:resultType,value:List(PEOPLE))),includeFiltersInResponse:false),count:{count})&queryId=voyagerSearchDashClusters.66adc6056cf4138949ca5dcb31bb1749"
    company_id_ep = "https://www.linkedin.com/voyager/api/organization/companies?q=universalName&universalName="
    company_search_ep = "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashClusters.02af3bc8bc85a169bb76bb4805d05759&queryName=SearchClusterCollection&variables=(query:(flagshipSearchIntent:SEARCH_SRP,keywords:{company},includeFiltersInResponse:false,queryParameters:(keywords:List({company}),resultType:List(COMPANIES))),count:10,origin:GLOBAL_SEARCH_HEADER,start:0)"
    location_id_ep = "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashReusableTypeahead.57a4fa1dd92d3266ed968fdbab2d7bf5&queryName=SearchReusableTypeaheadByType&variables=(query:(showFullLastNameForConnections:false,typeaheadFilterQuery:(geoSearchTypes:List(MARKET_AREA,COUNTRY_REGION,ADMIN_DIVISION_1,CITY))),keywords:{location},type:GEO,start:0)"
    public_user_id_ep = (
        "https://www.linkedin.com/voyager/api/identity/profiles/{user_id}/profileView"
    )
    connections_ep = "https://www.linkedin.com/voyager/api/graphql?queryId=voyagerSearchDashClusters.dfcd3603c2779eddd541f572936f4324&queryName=SearchClusterCollection&variables=(query:(queryParameters:(resultType:List(FOLLOWERS)),flagshipSearchIntent:MYNETWORK_CURATION_HUB,includeFiltersInResponse:true),count:50,origin:CurationHub,start:{offset})"
    block_user_ep = "https://www.linkedin.com/voyager/api/voyagerTrustDashContentReportingForm?action=entityBlock"
    connect_to_user_ep = "https://www.linkedin.com/voyager/api/voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2&decorationId=com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-1"

    def __init__(self, session: requests.Session):
        self.session = session
        (
            self.company_id,
            self.staff_count,
            self.num_staff,
            self.company_name,
            self.domain,
            self.max_results,
            self.search_term,
            self.location,
            self.raw_location,
        ) = (None, None, None, None, None, None, None, None, None)
        self.on_block = False
        self.connect_block = False
        self.certs = CertificationFetcher(self.session)
        self.skills = SkillsFetcher(self.session)
        self.employees = EmployeeFetcher(self.session)
        self.schools = SchoolsFetcher(self.session)
        self.experiences = ExperiencesFetcher(self.session)
        self.bio = EmployeeBioFetcher(self.session)
        self.languages = LanguagesFetcher(self.session)
        self.contact = ContactInfoFetcher(self.session)

        self.request_delay_min = 1
        self.request_delay_max = 3
        self.profile_fetch_count = 0
        self.max_profiles_before_break = 15

    def search_companies(self, company_name: str):
        """Get the company id and staff count from the company name."""

        company_search_ep = self.company_search_ep.format(company=quote(company_name))
        self.session.headers["x-li-graphql-pegasus-client"] = "true"
        res = self.session.get(company_search_ep)
        self.session.headers.pop("x-li-graphql-pegasus-client", "")
        if not res.ok:
            raise Exception(
                f"Failed to search for company {company_name}",
                res.status_code,
                res.text[:200],
            )
        logger.debug(
            f"Searched companies for name '{company_name}' - res code {res.status_code}-"
        )
        companies = res.json()["data"]["searchDashClustersByAll"]["elements"]

        err_msg = f"No companies found for name {company_name}"
        if len(companies) < 2:
            raise Exception(err_msg)
        try:
            num_results = companies[0]["items"][0]["item"]["simpleTextV2"]["text"][
                "text"
            ]
            first_company = companies[1]["items"][0]["item"].get("entityResult")
            if not first_company and len(companies) > 2:
                first_company = companies[2]["items"][0]["item"].get("entityResult")
            if not first_company:
                raise Exception(err_msg)

            company_link = first_company["navigationUrl"]
            company_name_id = unquote(
                re.search(r"/company/([^/]+)", company_link).group(1)
            )
            company_name_new = first_company["title"]["text"]
        except Exception as e:
            raise Exception(
                f"Failed to load json in search_companies {str(e)}, Response: {res.text[:200]}"
            )

        logger.info(
            f"Searched company {company_name} on LinkedIn and were {num_results}, using first result with company name - '{company_name_new}' and company id - '{company_name_id}'"
        )
        return company_name_id

    def fetch_or_search_company(self, company_name):
        """Fetch the company details by name, or search if not found."""
        res = self.session.get(f"{self.company_id_ep}{company_name}")

        if res.status_code not in (200, 404):
            formatted_name = self.format_company_name_for_linkedin(company_name)
            res = self.session.get(f"{self.company_id_ep}{formatted_name}")
            if res.status_code not in (200, 404):
                raise Exception(
                    f"Failed to find company {company_name}",
                    res.status_code,
                    res.text[:200],
                )
            elif res.status_code == 404:
                logger.info(
                    f"Failed to directly use company '{company_name}' as company id, now searching for the company"
                )
                company_name = self.search_companies(formatted_name)
                encoded_name = quote(company_name, safe='')
                res = self.session.get(f"{self.company_id_ep}{encoded_name}")
                if res.status_code != 200:
                    raise Exception(
                        f"Failed to find company after performing a direct and generic search for {company_name}",
                        res.status_code,
                        res.text[:200],
                    )

        elif res.status_code == 404:
            logger.info(
                f"Failed to directly use company '{company_name}' as company id, now searching for the company"
            )
            company_name = self.search_companies(company_name)
            res = self.session.get(f"{self.company_id_ep}{company_name}")
            if res.status_code != 200:
                raise Exception(
                    f"Failed to find company after performing a direct and generic search for {company_name}",
                    res.status_code,
                    res.text[:200],
                )

        if not res.ok:
            logger.debug(f"res code {res.status_code} - fetched company ")
        return res

    def _get_company_id_and_staff_count(self, company_name: str):
        """Extract company id and staff count from the company details."""
        res = self.fetch_or_search_company(company_name)

        try:
            response_json = res.json()
        except json.decoder.JSONDecodeError:
            logger.debug(res.text[:200])
            raise Exception(
                f"Failed to load json in get_company_id_and_staff_count {res.text[:200]}"
            )

        company = response_json["elements"][0]
        self.domain = (
            utils.extract_base_domain(company["companyPageUrl"])
            if company.get("companyPageUrl")
            else None
        )
        staff_count = company["staffCount"]
        company_id = company["trackingInfo"]["objectUrn"].split(":")[-1]
        company_name = company["universalName"]

        logger.info(f"Found company '{company_name}' with {staff_count} staff")
        return company_id, company_name, staff_count

    def parse_staff(self, elements: list[dict]):
        """Parse the staff from the search results"""
        staff = []

        for elem in elements:
            for card in elem.get("items", []):
                person = card.get("item", {}).get("entityResult", {})
                if not person:
                    continue
                pattern = (
                    r"urn:li:fsd_profile:([^,]+),(?:SEARCH_SRP|MYNETWORK_CURATION_HUB)"
                )
                match = re.search(pattern, person["entityUrn"])
                linkedin_id = match.group(1) if match else None
                person_urn = person["trackingUrn"].split(":")[-1]

                name = person["title"]["text"].strip()
                headline = (
                    person.get("primarySubtitle", {}).get("text", "")
                    if person.get("primarySubtitle")
                    else ""
                )
                profile_link = person["navigationUrl"].split("?")[0]
                staff.append(
                    Staff(
                        urn=person_urn,
                        id=linkedin_id,
                        name=name,
                        headline=headline,
                        search_term=" - ".join(
                            filter(
                                None,
                                [
                                    self.company_name,
                                    self.search_term,
                                    self.raw_location,
                                ],
                            )
                        ),
                        profile_link=profile_link,
                    )
                )
        return staff

    def format_company_name_for_linkedin(self, company_name):
        formatted = company_name.lower()
        formatted = formatted.replace(',', ' ')
        formatted = formatted.replace('.', ' ')
        formatted = formatted.replace('&', 'and')
        formatted = formatted.strip('-')
        return formatted

    def fetch_staff(self, offset: int, search_by_title: bool = False):
        """Fetch the staff using LinkedIn search

        Args:
            offset: Pagination offset
            search_by_title: If True, search only in job titles. If False, search in all profile fields
        """
        # Randomize headers before each request
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        ]
        self.session.headers["User-Agent"] = random.choice(user_agents)

        # Base parameters listesi oluştur
        query_parameters = []

        # Company parametresi
        if self.company_id:
            query_parameters.append(f"(key:currentCompany,value:List({self.company_id}))")

        # Location parametresi
        if self.location:
            query_parameters.append(f"(key:geoUrn,value:List({self.location}))")

        # Search term parametresi
        if self.search_term:
            if search_by_title:
                # Title araması için - quote kullanmadan direkt string olarak ekle
                # LinkedIn API'si title için tırnak işareti bekliyor
                query_parameters.append(f'(key:title,value:List("{self.search_term}"))')
            # else kısmı keywords için aşağıda ele alınacak

        # Result type her zaman PEOPLE
        query_parameters.append("(key:resultType,value:List(PEOPLE))")

        # Query parameters'ı birleştir
        query_params_str = ",".join(query_parameters)

        # Keywords parametresi (title araması değilse)
        keywords_str = ""
        if self.search_term and not search_by_title:
            # Keywords query dışında, ana query'de yer alır
            keywords_str = f"keywords:{quote(self.search_term)},"

        # URL'yi oluştur
        # LinkedIn'in beklediği format:
        # variables=(start:X,query:(flagshipSearchIntent:SEARCH_SRP,queryParameters:List(...),keywords:...),count:X)

        if keywords_str:
            # Keywords varsa
            query_part = f"query:(flagshipSearchIntent:SEARCH_SRP,{keywords_str}queryParameters:List({query_params_str}),includeFiltersInResponse:false)"
        else:
            # Keywords yoksa
            query_part = f"query:(flagshipSearchIntent:SEARCH_SRP,queryParameters:List({query_params_str}),includeFiltersInResponse:false)"

        # Final URL
        ep = f"https://www.linkedin.com/voyager/api/graphql?variables=(start:{offset},{query_part},count:50)&queryId=voyagerSearchDashClusters.66adc6056cf4138949ca5dcb31bb1749"

        # Debug için URL'yi logla
        logger.debug(f"Request URL: {ep}")

        res = self.session.get(ep)

        if not res.ok:
            logger.debug(f"employees, status code - {res.status_code}")
            logger.debug(f"Response text: {res.text}")

        if res.status_code == 400:
            # Bad Request durumunda daha detaylı hata mesajı
            logger.error(f"Bad Request - URL: {ep}")
            logger.error(f"Response: {res.text}")
            raise BadCookies("Bad Request - Check URL format or outdated login")
        elif res.status_code == 429:
            raise TooManyRequests("429 Too Many Requests")

        if not res.ok:
            return None, 0

        try:
            res_json = res.json()
        except json.decoder.JSONDecodeError:
            logger.debug(f"JSON decode error: {res.text}")
            return None, 0

        try:
            elements = res_json["data"]["searchDashClustersByAll"]["elements"]
            total_count = res_json["data"]["searchDashClustersByAll"]["metadata"][
                "totalResultCount"
            ]
        except (KeyError, IndexError, TypeError):
            logger.debug(f"Response structure error: {res_json}")
            return None, 0

        new_staff = self.parse_staff(elements) if elements else []
        return new_staff, total_count


    def fetch_connections_page(self, offset: int):
        self.session.headers["x-li-graphql-pegasus-client"] = "true"
        res = self.session.get(self.connections_ep.format(offset=offset))
        self.session.headers.pop("x-li-graphql-pegasus-client", "")
        if not res.ok:
            logger.debug(f"employees, status code - {res.status_code}")
        if res.status_code == 400:
            raise BadCookies("Outdated login, delete the session file to log in again")
        elif res.status_code == 429:
            raise TooManyRequests("429 Too Many Requests")
        if not res.ok:
            return
        try:
            res_json = res.json()
        except json.decoder.JSONDecodeError:
            logger.debug(res.text)
            return

        try:
            elements = res_json["data"]["searchDashClustersByAll"]["elements"]
            total_count = res_json["data"]["searchDashClustersByAll"]["metadata"][
                "totalResultCount"
            ]

        except (KeyError, IndexError, TypeError):
            logger.debug(res_json)
            return

        new_staff = self.parse_staff(elements) if elements else []
        return new_staff, total_count

    def scrape_connections(
        self,
        max_results: int = 10**8,
        extra_profile_data: bool = False,
    ):
        self.search_term = "connections"
        staff_list: list[Staff] = []

        try:
            initial_staff, total_search_result_count = self.fetch_connections_page(0)
            if initial_staff:
                staff_list.extend(initial_staff)

            self.num_staff = min(total_search_result_count, max_results)
            for offset in range(50, self.num_staff, 50):
                staff, _ = self.fetch_connections_page(offset)
                logger.debug(
                    f"Connections from search: {len(staff)} new, {len(staff_list) + len(staff)} total"
                )
                if not staff:
                    break
                staff_list.extend(staff)
        except (BadCookies, TooManyRequests) as e:
            self.on_block = True
            logger.error(f"Exiting early due to fatal error: {str(e)}")
            return staff_list[:max_results]

        reduced_staff_list = staff_list[:max_results]

        non_restricted = list(
            filter(lambda x: x.name != "LinkedIn Member", reduced_staff_list)
        )

        if extra_profile_data:
            try:
                for i, employee in enumerate(non_restricted, start=1):
                    self.fetch_all_info_for_employee(employee, i)
            except TooManyRequests as e:
                logger.error(str(e))
        return reduced_staff_list

    def fetch_location_id(self):
        """Fetch the location id for the location to be used in LinkedIn search"""
        ep = self.location_id_ep.format(location=quote(self.raw_location))
        res = self.session.get(ep)
        try:
            res_json = res.json()
        except json.decoder.JSONDecodeError:
            if res.reason == "INKApi Error":
                raise Exception(
                    "Delete session file and log in again",
                    res.status_code,
                    res.text[:200],
                    res.reason,
                )
            raise GeoUrnNotFound(
                "Failed to send request to get geo id",
                res.status_code,
                res.text[:200],
                res.reason,
            )

        try:
            elems = res_json["data"]["searchDashReusableTypeaheadByType"]["elements"]
        except (KeyError, IndexError, TypeError):
            raise GeoUrnNotFound("Failed to locate geo id", res_json[:200])

        geo_id = None
        if elems:
            urn = elems[0]["trackingUrn"]
            m = re.search("urn:li:geo:(.+)", urn)
            if m:
                geo_id = m.group(1)
        if not geo_id:
            raise GeoUrnNotFound("Failed to parse geo id")
        self.location = geo_id

    def scrape_staff(
            self,
            company_name: str | None,
            search_term: str,
            location: str,
            extra_profile_data: bool,
            max_results: int,
            block: bool,
            connect: bool,
    ) -> tuple["pd.DataFrame", dict]:
        """Main function entry point to scrape LinkedIn staff"""
        import pandas as pd

        self.search_term = search_term
        self.company_name = company_name
        self.max_results = max_results
        self.raw_location = location
        self.company_id = None
        self.returned_company_name = None

        # Metadata dictionary'sini başlat
        metadata = {
            "company_name": company_name,
            "total_staff_in_company": None,
            "total_staff_in_location": None,
            "location": location,
            "results_collected": 0,
            "error_message": None,
            "search_terms_used": []  # Hangi search term'lerin kullanıldığını takip et
        }

        if self.company_name:
            self.company_id, self.returned_company_name, staff_count = self._get_company_id_and_staff_count(
                company_name
            )
            metadata["total_staff_in_company"] = staff_count
            metadata["company_name"] = self.returned_company_name

        staff_list: list[Staff] = []
        seen_profile_ids = set()  # Duplicate'leri önlemek için

        if self.raw_location:
            try:
                self.fetch_location_id()
            except GeoUrnNotFound as e:
                logger.error(str(e))
                metadata["error_message"] = str(e)
                empty_df = pd.DataFrame()
                return empty_df, metadata

        # Ek search term'ler listesi
        additional_search_terms = [
            "Office", "Hospitality", "Workplace", "Catering",
            "Happiness","Culture", "Conference Center", "People", "Employee", "Corporate Services", "Food Service"
        ]

        # Helper function to add unique staff to list
        def add_unique_staff(new_staff, staff_list, seen_ids):
            """Add only unique staff members to the list"""
            added_count = 0
            for staff in new_staff:
                if staff.id not in seen_ids:
                    staff_list.append(staff)
                    seen_ids.add(staff.id)
                    added_count += 1
            return added_count

        try:
            initial_staff, total_count = self.fetch_staff(0)
            if initial_staff and total_count < 1000:
                add_unique_staff(initial_staff, staff_list, seen_profile_ids)

            metadata["total_staff_in_location"] = total_count
            location_str = f", location: '{location}'" if location else ""

            logger.info(
                f"1) Search results for company: '{company_name}'{location_str} - {total_count:,} staff"
            )

            if total_count < 20:
                error_msg = f"Found company '{metadata["company_name"]}' with {staff_count} staff.\nCompany '{metadata["company_name"]}' has only {total_count} staff members in {location if location else 'total'}. Minimum 20 staff required for processing."
                metadata["error_message"] = error_msg
                raise ValueError(error_msg)

            # Eğer total_count 1000 veya daha fazla ise, SADECE search term'lerle ara
            if total_count >= 1000:
                logger.info(
                    f"Result limit (1000) reached. Will ONLY use specific search terms, skipping general search...")

                # Ek search term'lerle ara
                for term in additional_search_terms:
                    # if len(staff_list) >= max_results:
                    #     logger.info(f"Reached max_results limit ({max_results}). Stopping search.")
                    #     break

                    logger.info(f"Searching with term: '{term}'")

                    # Search term'i güncelle
                    original_search_term = self.search_term
                    self.search_term = term

                    try:
                        # Her search term için arama yap
                        term_staff, term_count = self.fetch_staff(0, True)

                        if term_staff and term_count > 0:
                            added = add_unique_staff(term_staff, staff_list, seen_profile_ids)
                            logger.info(f"--------------- Found {term_count} results for '{term}', added {added} unique profiles")

                            # Bu search term için TÜM sayfaları al (limit yok)
                            for offset in range(50, term_count, 50):
                                # if len(staff_list) >= max_results:
                                #     break

                                more_staff, _ = self.fetch_staff(offset)
                                if more_staff:
                                    added = add_unique_staff(more_staff, staff_list, seen_profile_ids)
                                    logger.debug(f"--------------- Added {added} unique staff for term '{term}'")
                                else:
                                    break

                                # Rate limiting için küçük delay
                                time.sleep(random.uniform(0.5, 1.5))

                            metadata["search_terms_used"].append(term)

                    except Exception as e:
                        logger.warning(f"Error searching with term '{term}': {str(e)}")
                        continue
                    finally:
                        # Original search term'e geri dön
                        self.search_term = original_search_term

                    # Her search term arasında delay
                    time.sleep(random.uniform(2, 4))

            else:
                # Normal akış (1000'den az sonuç varsa)
                self.num_staff = min(total_count, max_results, 1000)
                for offset in range(50, self.num_staff, 50):
                    staff, _ = self.fetch_staff(offset)
                    if not staff:
                        break
                    add_unique_staff(staff, staff_list, seen_profile_ids)
                    logger.debug(f"Staff members from search: {len(staff)} new, {len(staff_list)} total")

                metadata["search_terms_used"].append(search_term if search_term else "general")

            logger.info(
                f"2) Total unique results collected for company: '{company_name}'{location_str} - {len(staff_list)} results"
            )

        except (BadCookies, TooManyRequests) as e:
            self.on_block = True
            error_msg = f"Exiting early due to fatal error: {str(e)}"
            logger.error(error_msg)
            metadata["error_message"] = error_msg
            reduced_staff_list = staff_list[:max_results]
            metadata["results_collected"] = len(reduced_staff_list)

            if reduced_staff_list:
                df = pd.DataFrame([staff.__dict__ for staff in reduced_staff_list])
            else:
                df = pd.DataFrame()
            return df, metadata

        reduced_staff_list = staff_list[:max_results]
        metadata["results_collected"] = len(reduced_staff_list)

        non_restricted = list(
            filter(lambda x: x.name != "LinkedIn Member", reduced_staff_list)
        )

        if extra_profile_data:
            try:
                for i, employee in enumerate(non_restricted, start=1):
                    self.fetch_all_info_for_employee(employee, i)
                    if block:
                        self.block_user(employee)
                    elif connect:
                        self.connect_user(employee)

            except TooManyRequests as e:
                logger.error(str(e))
                metadata["error_message"] = str(e)

        # Staff listini DataFrame'e çevir - to_dict() metodunu kullan
        if reduced_staff_list:
            staff_dicts = [staff.to_dict() for staff in reduced_staff_list]
            df = pd.DataFrame(staff_dicts)
        else:
            df = pd.DataFrame()

        return df, metadata

    def _safe_delay(self):
        """Add random delay to avoid detection"""
        delay = random.uniform(self.request_delay_min, self.request_delay_max)
        time.sleep(delay)

    def fetch_all_info_for_employee(self, employee: Staff, index: int):
        """Simultaniously fetch all the data for an employee"""

        self._safe_delay()  # Random delay

        # Her 15 profilden sonra uzun mola
        if index % 15 == 0 and index > 0:
            logger.info(f"Taking a 10-second break after {index} profiles...")
            time.sleep(7)

        logger.info(
            f"Fetching data for account {employee.id} {index:>4} / {self.num_staff} - {employee.profile_link}"
        )

        try:
            self.employees.fetch_employee(employee, self.domain)
            time.sleep(random.uniform(1, 3))

            # 2. Bio info
            self.bio.fetch_employee_bio(employee)
            time.sleep(random.uniform(1., 2))

            # 3. Experiences
            self.experiences.fetch_experiences(employee)
            time.sleep(1)

            # 4. skills
            # self.skills.fetch_skills(employee)
            # time.sleep(0.75)

        except Exception as e:
            logger.warning(f"Error fetching data for {employee.id}: {e}")
            # Hata durumunda devam et
            pass

        if employee.is_connection:
            self.contact.fetch_contact_info(employee)



        # task_functions = [
        #     (self.employees.fetch_employee, (employee, self.domain), "employee"),
        #     (self.skills.fetch_skills, (employee,), "skills"),
        #     (self.experiences.fetch_experiences, (employee,), "experiences"),
        #     (self.certs.fetch_certifications, (employee,), "certifications"),
        #     (self.schools.fetch_schools, (employee,), "schools"),
        #     (self.bio.fetch_employee_bio, (employee,), "bio"),
        #     (self.languages.fetch_languages, (employee,), "languages"),
        # ]
        #
        # with ThreadPoolExecutor(max_workers=len(task_functions)) as executor:
        #     tasks = {
        #         executor.submit(func, *args): name
        #         for func, args, name in task_functions
        #     }
        #
        #     for future in as_completed(tasks):
        #         result = future.result()
        #
        # if employee.is_connection:
            self.contact.fetch_contact_info(employee)

    def fetch_user_profile_data_from_public_id(self, user_id: str, key: str):
        """Fetches data given the public LinkedIn user id"""
        endpoint = self.public_user_id_ep.format(user_id=user_id)
        response = self.session.get(endpoint)

        try:
            response_json = response.json()
        except json.decoder.JSONDecodeError:
            logger.debug(response.text[:200])
            raise Exception(
                f"Failed to load JSON from endpoint",
                response.status_code,
                response.reason,
            )

        keys = {
            "user_id": ("positionView", "profileId"),
            "company_id": (
                "positionView",
                "elements",
                0,
                "company",
                "miniCompany",
                "universalName",
            ),
        }

        try:
            data = response_json
            for k in keys[key]:
                data = data[k]
            urn = response_json["profile"]["miniProfile"]["objectUrn"].split(":")[-1]
            return data, urn
        except (KeyError, TypeError, IndexError) as e:
            logger.warning(f"Failed to find user_id {user_id}")
            if key == "user_id":
                return ""
            raise Exception(f"Failed to fetch '{key}' for user_id {user_id}: {e}")

    def block_user(self, employee: Staff) -> None:
        """Block a user on LinkedIn given their urn"""
        if employee.urn == "headless":
            return
        self.session.headers["Content-Type"] = (
            "application/x-protobuf2; symbol-table=voyager-20757"
        )

        urn_string = f"urn:li:member:{employee.urn}"
        length_byte = bytes([len(urn_string)])
        body = b"\x00\x01\x14\nblockeeUrn\x14" + length_byte + urn_string.encode()

        res = self.session.post(
            self.block_user_ep,
            data=body,
        )
        self.session.headers.pop("Content-Type", "")

        if res.ok:
            logger.info(f"Successfully blocked user {employee.id}")
        elif res.status_code == 403:
            logger.warning(
                f"Failed to block user - status code 403, one possible reason is you have alread blocked/unblocked this person in past 48 hours and on cooldown: {employee.profile_link}"
            )
        else:
            logger.warning(
                f"Failed to block user - status code {res.status_code} {employee.id}: {employee.name}"
            )

    def connect_user(self, employee: Staff) -> None:
        """Connects with a user on LinkedIn given their profile id"""
        if self.connect_block:
            return logger.info(
                f"Skipping connection request for user due to previou block: {employee.id} - {employee.profile_link} "
            )
        if employee.urn == "headless":
            return
        if employee.is_connection != "no":
            return logger.info(
                f"Already connected or pending connection request to user {employee.id} - {employee.profile_link}"
            )
        self.session.headers["Content-Type"] = (
            "application/x-protobuf2; symbol-table=voyager-20757"
        )
        body = (
            b"\x00\x01\x03\xe2\x05\x00\x01\x03\xd3w\x00\x01\x03\xd5\x06\x14:urn:li:fsd_profile:"
            + employee.id.encode()
        )

        res = self.session.post(
            self.connect_to_user_ep,
            data=body,
        )
        self.session.headers.pop("Content-Type", "")

        if res.ok:
            logger.info(
                f"Successfully sent connection request to user {employee.id} - {employee.profile_link}"
            )
        elif res.status_code == 429:
            self.connect_block = True
            logger.warning(
                f"Failed to connect to user - status code 429 - pausing connection requests for this scrape: {employee.id} - {employee.profile_link}"
            )
        else:
            logger.warning(
                f"Failed to connect to user - status code {res.status_code} {employee.id} -{employee.profile_link}"
            )
